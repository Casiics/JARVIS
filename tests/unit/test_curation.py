"""Four-stage context curation engine (jarvis_core.curation)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from jarvis_core.audit.chain import AuditWriter, InMemoryAuditStore, verify_chain
from jarvis_core.contracts.audit import AuditAction, AuditDecision, AuditLogEntry
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import Taint
from jarvis_core.curation.models import (
    CategoryRule,
    CollectedItem,
    CurationConfig,
    Outcome,
    Proposal,
)
from jarvis_core.curation.pipeline import CurationPipeline, ReviewNotFound
from jarvis_core.curation.similarity import fingerprint, similarity, tokens
from jarvis_core.curation.store import FileCurationStore, open_reviews

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)

CONFIG = CurationConfig(
    domain=Domain.CONTENT,
    default_category="general",
    categories=(
        CategoryRule(name="dev-tooling", tags=("mod-self-evolution",), keywords=("ripgrep", "cli")),
        CategoryRule(
            name="market-notes",
            tags=("mod-trading",),
            keywords=("qqq", "spy"),
            min_signal=0.4,
            auto_store_at=0.85,
        ),
        CategoryRule(name="general", auto_store_at=0.8),
    ),
)

RIPGREP = "Use ripgrep instead of grep to search large codebases quickly from the terminal."
RIPGREP_REPOST = "Ripgrep searches large codebases quickly; use it instead of grep in the terminal."
QQQ = "A QQQ opening range breakout strategy that waits for the first 15 minutes of trading."


def item(
    n: int,
    summary: str = RIPGREP,
    *,
    source: str | None = None,
    signal: float = 0.9,
    tags: tuple[str, ...] = ("mod-self-evolution",),
    facts: tuple[str, ...] = ("Install ripgrep", "Replace grep -r with rg"),
    domain: Domain = Domain.CONTENT,
    classification: DataClassification = DataClassification.PUBLIC,
    flags: tuple[str, ...] = (),
) -> CollectedItem:
    return CollectedItem(
        item_id=f"reel:{n}",
        domain=domain,
        classification=classification,
        taint=Taint.EXTERNAL_CONTENT,
        source_ref=source or f"https://www.instagram.com/reel/REEL{n:05d}/",
        collected_at=NOW,
        title=summary[:60],
        summary=summary,
        facts=facts,
        tags=tags,
        signal=signal,
        flags=flags,
        content_hash=fingerprint(summary, *facts),
    )


def make(tmp_path: Path) -> tuple[CurationPipeline, FileCurationStore, InMemoryAuditStore]:
    audit = InMemoryAuditStore()
    store = FileCurationStore(tmp_path / "curation")
    pipeline = CurationPipeline(
        config=CONFIG,
        store=store,
        audit=AuditWriter(audit).write,
        actor="spiffe://jarvis.home/content/curation",
        clock=lambda: NOW,
    )
    return pipeline, store, audit


def entries_of(audit: InMemoryAuditStore) -> list[AuditLogEntry]:
    return [AuditLogEntry.model_validate_json(e.payload) for e in audit.entries]


def test_similarity_catches_paraphrased_repost() -> None:
    assert similarity(tokens(RIPGREP), tokens(RIPGREP_REPOST)) >= CONFIG.duplicate_similarity
    assert similarity(tokens(RIPGREP), tokens(QQQ)) < 0.2


def test_stage1_is_idempotent_and_refuses_foreign_and_secret(tmp_path: Path) -> None:
    pipeline, store, audit = make(tmp_path)
    batch = [
        item(1),
        item(2, domain=Domain.TRADING),
        item(3, classification=DataClassification.SECRET),
    ]
    assert pipeline.collect(batch, "t1") == (1, 2)
    assert pipeline.collect(batch[:1], "t2") == (0, 0)
    assert [i.item_id for i in store.items()] == ["reel:1"]
    denied = [e for e in entries_of(audit) if e.decision is AuditDecision.DENY]
    assert {e.policy_id for e in denied} == {"foreign_domain", "secret_never_context"}


def test_missing_label_fails_closed_to_private() -> None:
    raw = item(1).model_dump()
    del raw["classification"]
    assert CollectedItem.model_validate(raw).classification is DataClassification.PRIVATE


def test_full_run_dedups_categorises_and_stores(tmp_path: Path) -> None:
    pipeline, store, audit = make(tmp_path)
    counts = pipeline.run(
        [
            item(1),
            item(2, RIPGREP_REPOST, facts=("Install ripgrep", "Use rg --hidden for dotfiles")),
            item(3, RIPGREP, source="https://www.instagram.com/reel/REEL00001/"),
            item(4, QQQ, tags=("mod-trading",), signal=0.7, facts=("Wait 15 minutes",)),
        ]
    )
    assert counts.collected == 4
    assert counts.digests == 2
    assert counts.duplicates_dropped == 2
    [tooling] = store.entries("dev-tooling")
    assert len(tooling.source_refs) == 2  # item 3 re-posted the same reel as item 1
    assert "Use rg --hidden for dotfiles" in tooling.facts
    # market notes need 0.85: a single 0.7 source waits for the owner
    assert counts.stored == 1 and counts.review == 1
    [brief] = open_reviews(store.decisions())
    assert brief.category == "market-notes" and brief.proposal is Proposal.STORE

    assert verify_chain(audit.entries) is None
    stages = [e.target for e in entries_of(audit)]
    assert stages.count("curation.collect") == 4
    assert stages.count("curation.consolidate") == 2
    assert stages.count("curation.prepare") == 2
    assert stages.count("curation.decide") == 2
    for entry in entries_of(audit):
        assert entry.content_hash is not None and entry.content_ref is not None


def test_second_run_only_handles_new_items(tmp_path: Path) -> None:
    pipeline, store, _ = make(tmp_path)
    pipeline.run([item(1)])
    counts = pipeline.run([item(1)])
    assert counts.collected == 0 and counts.digests == 0
    assert len(store.entries()) == 1


def test_known_content_is_discarded_and_extension_is_merged(tmp_path: Path) -> None:
    pipeline, store, _ = make(tmp_path)
    pipeline.run([item(1)])
    again = pipeline.run([item(2, source="https://www.instagram.com/reel/OTHER0001/")])
    assert again.discarded == 1  # same text and facts, different reel: nothing new to keep
    more = pipeline.run(
        [
            item(
                3,
                RIPGREP_REPOST,
                source="https://www.instagram.com/reel/OTHER0002/",
                facts=("Install ripgrep", "Pair rg with fzf for fuzzy search"),
            )
        ]
    )
    assert more.merged == 1
    [entry] = store.entries("dev-tooling")
    assert entry.version == 2
    assert "Pair rg with fzf for fuzzy search" in entry.facts
    assert len(entry.source_refs) == 2


def test_low_signal_is_discarded(tmp_path: Path) -> None:
    pipeline, store, _ = make(tmp_path)
    counts = pipeline.run([item(1, QQQ, tags=("mod-trading",), signal=0.1)])
    assert counts.discarded == 1
    assert store.entries() == []


def test_flagged_items_always_wait_for_review(tmp_path: Path) -> None:
    pipeline, store, _ = make(tmp_path)
    counts = pipeline.run([item(1, signal=1.0, flags=("ignore_previous_instructions",))])
    assert counts.review == 1 and store.entries() == []


def test_over_classified_material_waits_for_review(tmp_path: Path) -> None:
    pipeline, _, _ = make(tmp_path)
    counts = pipeline.run([item(1, classification=DataClassification.PRIVATE)])
    assert counts.review == 1


def test_owner_review_accepts_and_rejects(tmp_path: Path) -> None:
    pipeline, store, audit = make(tmp_path)
    pipeline.run(
        [
            item(1, QQQ, tags=("mod-trading",), signal=0.7, facts=("Wait 15 minutes",)),
            item(2, signal=1.0, flags=("hidden_instruction",)),
        ]
    )
    reviews = open_reviews(store.decisions())
    assert len(reviews) == 2
    market = next(b for b in reviews if b.category == "market-notes")
    flagged = next(b for b in reviews if b.category == "dev-tooling")
    assert pipeline.review(market.brief_id, True, by="user:owner").outcome is Outcome.STORED
    assert pipeline.review(flagged.brief_id, False, by="user:owner").outcome is Outcome.DISCARDED
    assert open_reviews(store.decisions()) == []
    assert [e.category for e in store.entries()] == ["market-notes"]
    with pytest.raises(ReviewNotFound):
        pipeline.review(market.brief_id, True, by="user:owner")
    approvals = [e for e in entries_of(audit) if e.action is AuditAction.APPROVAL]
    assert [e.actor for e in approvals] == ["user:owner", "user:owner"]


def test_config_rejects_unknown_default_and_inverted_thresholds() -> None:
    with pytest.raises(ValueError, match="default_category"):
        CurationConfig(domain=Domain.CONTENT, default_category="x", categories=CONFIG.categories)
    with pytest.raises(ValueError, match="auto_discard_below"):
        CategoryRule(name="x", auto_store_at=0.3, auto_discard_below=0.5)
