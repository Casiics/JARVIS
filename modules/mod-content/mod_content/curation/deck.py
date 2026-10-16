"""Bridge between content's curation reviews and the owner's deck.

Open reviews go to core as ContextReviewRequested; the owner's ContextReviewDecided answers come
back and are applied here, by content, to content's own store. Reviews travel through their own
mailbox root so they never race the reel worker or core's orchestrator for letters.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jarvis_core.boundary.mailbox import Mailbox, MailboxError
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import ContextReviewDecided, ContextReviewRequested
from jarvis_core.curation.models import DecisionBrief, Proposal
from jarvis_core.curation.pipeline import CurationPipeline, ReviewNotFound
from jarvis_core.curation.store import CurationStore, open_reviews

REVIEW_MAILBOX = "context-review"
_PROPOSAL: dict[Proposal, Literal["store", "merge", "discard"]] = {
    Proposal.STORE: "store",
    Proposal.MERGE: "merge",
    Proposal.DISCARD: "discard",
}


def review_mailbox(boundary_dir: Path) -> Mailbox:
    return Mailbox(boundary_dir / REVIEW_MAILBOX, Domain.CONTENT)


@dataclass(frozen=True)
class DeckSync:
    sent: int = 0
    applied: int = 0
    not_sendable: int = 0


class DeckReviews:
    def __init__(
        self, pipeline: CurationPipeline, store: CurationStore, mailbox: Mailbox, sent_file: Path
    ) -> None:
        if mailbox.me is not Domain.CONTENT:
            raise ValueError("the curation deck bridge runs in content")
        self._pipeline = pipeline
        self._store = store
        self._mailbox = mailbox
        self._sent_file = sent_file

    def sync(self) -> DeckSync:
        """Apply the owner's answers first, then send reviews the deck has not seen yet."""
        applied = 0
        for letter in self._mailbox.receive(Domain.CORE):
            event = letter.event
            if isinstance(event, ContextReviewDecided):
                try:
                    self._pipeline.review(
                        event.brief_id, event.accept, by=event.decided_by, note=event.note
                    )
                    applied += 1
                except ReviewNotFound:
                    pass  # already decided in the terminal; the deck's answer is moot
            self._mailbox.ack(letter)

        sent_ids = self._sent()
        sent = skipped = 0
        for brief in open_reviews(self._store.decisions()):
            if brief.brief_id in sent_ids:
                continue
            try:
                self._mailbox.send(to_event(brief))
            except (MailboxError, ValueError):
                # Over-classified material stays a terminal review; it never reaches core.
                skipped += 1
            else:
                sent += 1
            sent_ids.add(brief.brief_id)
        if sent or skipped:
            self._sent_file.parent.mkdir(parents=True, exist_ok=True)
            self._sent_file.write_text(json.dumps(sorted(sent_ids)), encoding="utf-8")
        return DeckSync(sent=sent, applied=applied, not_sendable=skipped)

    def _sent(self) -> set[str]:
        if not self._sent_file.exists():
            return set()
        return set(json.loads(self._sent_file.read_text(encoding="utf-8")))


def to_event(brief: DecisionBrief) -> ContextReviewRequested:
    d = brief.digest
    if d.classification.at_least(DataClassification.CONFIDENTIAL):
        raise ValueError("only PUBLIC or INTERNAL material is shown in the deck")
    return ContextReviewRequested(
        origin_domain=Domain.CONTENT,
        target_domain=Domain.CORE,
        classification=d.classification,
        taint=d.taint,
        brief_id=brief.brief_id,
        category=brief.category,
        proposal=_PROPOSAL[brief.proposal],
        confidence=brief.confidence,
        title=d.title[:200],
        summary=d.summary[:1500],
        facts=tuple(f[:300] for f in d.facts[:10]),
        reasons=tuple(r[:300] for r in brief.reasons[:8]),
        flags=tuple(f[:300] for f in d.flags[:10]),
        source_refs=tuple(s[:500] for s in d.source_refs[:10]),
    )
