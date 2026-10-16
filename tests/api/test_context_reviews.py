"""Context reviews in the deck: content asks, the owner answers, content applies the answer.

Core only relays a bounded event and the owner's decision; it never reads content's store.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from jarvis_core.api.context_reviews import ContextReviewDesk
from jarvis_core.audit.chain import AuditWriter, InMemoryAuditStore
from jarvis_core.boundary.mailbox import Mailbox, MailboxError
from jarvis_core.contracts.audit import AuditAction
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import ContextReviewRequested
from jarvis_core.curation.pipeline import CurationPipeline
from jarvis_core.curation.store import FileCurationStore, open_reviews
from jarvis_core.policy.flows import check_flow
from mod_content.curation.deck import DeckReviews, review_mailbox
from tests.api.test_deck_approvals import Deck
from tests.unit.test_curation import CONFIG, QQQ, item

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


@pytest.fixture
def deck(tmp_path: Path) -> Deck:
    d = Deck()
    d.deps.context_reviews = ContextReviewDesk(
        Mailbox(tmp_path / "boundary/context-review", Domain.CORE),
        tmp_path / "core/context-reviews.json",
        clock=lambda: NOW,
    )
    d.register()
    d.login()
    return d


def content(tmp_path: Path) -> tuple[CurationPipeline, FileCurationStore, DeckReviews]:
    store = FileCurationStore(tmp_path / "content/curation")
    pipeline = CurationPipeline(
        config=CONFIG,
        store=store,
        audit=AuditWriter(InMemoryAuditStore()).write,
        actor="spiffe://jarvis.home/content/curation",
        clock=lambda: NOW,
    )
    bridge = DeckReviews(
        pipeline,
        store,
        review_mailbox(tmp_path / "boundary"),
        tmp_path / "content/curation/deck-sent.json",
    )
    return pipeline, store, bridge


def test_review_round_trip_through_the_deck(deck: Deck, tmp_path: Path) -> None:
    pipeline, store, bridge = content(tmp_path)
    pipeline.run([item(1, QQQ, tags=("mod-trading",), signal=0.7, facts=("Wait 15 minutes",))])
    assert bridge.sync().sent == 1
    assert bridge.sync().sent == 0  # each review is sent once

    listing = deck.client.get("/api/content/context-reviews").json()
    [review] = listing["open"]
    assert review["category"] == "market-notes"
    assert review["title"].startswith("A QQQ opening range")
    assert review["facts"] == ["Wait 15 minutes"]

    res = deck.post(f"/api/content/context-reviews/{review['brief_id']}/decision", {"accept": True})
    assert res.status_code == 200, res.text
    assert res.json()["review"]["accepted"] is True
    again = deck.post(
        f"/api/content/context-reviews/{review['brief_id']}/decision", {"accept": True}
    )
    assert again.status_code == 409

    [entry] = [e for e in deck.audit if e.action is AuditAction.APPROVAL]
    assert entry.target == "content:context_review" and entry.content_ref == review["brief_id"]
    assert QQQ not in entry.model_dump_json()

    assert bridge.sync().applied == 1
    assert open_reviews(store.decisions()) == []
    [stored] = store.entries("market-notes")
    assert stored.title.startswith("A QQQ opening range")
    listing = deck.client.get("/api/content/context-reviews").json()
    assert listing["open"] == [] and listing["recent"][0]["accepted"] is True


def test_rejecting_in_the_deck_discards(deck: Deck, tmp_path: Path) -> None:
    pipeline, store, bridge = content(tmp_path)
    pipeline.run([item(1, signal=1.0, flags=("hidden_instruction",))])
    bridge.sync()
    [review] = deck.client.get("/api/content/context-reviews").json()["open"]
    assert review["flags"] == ["hidden_instruction"]
    deck.post(f"/api/content/context-reviews/{review['brief_id']}/decision", {"accept": False})
    bridge.sync()
    assert store.entries() == [] and open_reviews(store.decisions()) == []


def test_private_material_never_reaches_the_deck(deck: Deck, tmp_path: Path) -> None:
    pipeline, store, bridge = content(tmp_path)
    pipeline.run([item(1, classification=DataClassification.PRIVATE)])
    assert bridge.sync().not_sendable == 1
    assert deck.client.get("/api/content/context-reviews").json()["open"] == []
    assert len(open_reviews(store.decisions())) == 1  # still decidable in the terminal


def test_flow_policy_refuses_confidential_review_requests() -> None:
    event = ContextReviewRequested(
        origin_domain=Domain.CONTENT,
        target_domain=Domain.CORE,
        classification=DataClassification.CONFIDENTIAL,
        brief_id="br:" + "0" * 24,
        category="general",
        proposal="store",
        confidence=0.5,
        title="t",
        summary="s",
    )
    assert not check_flow(event, caller_domain=Domain.CONTENT).allowed
    assert not check_flow(event, caller_domain=Domain.TRADING).allowed
    with pytest.raises(MailboxError):
        Mailbox(Path("/nonexistent"), Domain.CONTENT).send(event)
    public = event.model_copy(update={"classification": DataClassification.PUBLIC})
    assert check_flow(public, caller_domain=Domain.CONTENT).allowed


def test_reviews_are_unavailable_without_a_desk(tmp_path: Path) -> None:
    d = Deck()
    d.register()
    d.login()
    assert d.client.get("/api/content/context-reviews").status_code == 503
