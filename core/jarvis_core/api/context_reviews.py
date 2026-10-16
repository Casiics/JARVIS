"""Context reviews in the deck: the owner decides what content's curation was unsure about.

Content sends a bounded ContextReviewRequested event; the desk keeps it until the owner
decides, then sends ContextReviewDecided back. Core never reads or writes content's context
store; content applies the decision itself.

Reviews travel through their own mailbox root (`<boundary>/context-review/`), because the reel
desk and the orchestrator consume every content-to-core letter in the main root.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, TypeAdapter

from jarvis_core.api.filestore import write_atomic
from jarvis_core.boundary.mailbox import Mailbox
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import ContextReviewDecided, ContextReviewRequested

MAX_KEPT_DECIDED = 50


class ContextReview(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request: ContextReviewRequested
    received_at: datetime
    decided_at: datetime | None = None
    accepted: bool | None = None
    decided_by: str | None = None


_REVIEWS = TypeAdapter(list[ContextReview])


class ReviewUnknown(Exception):
    """No open review with that id."""


class ContextReviewDesk:
    def __init__(self, mailbox: Mailbox, path: Path | None, clock: Callable[[], datetime]) -> None:
        if mailbox.me is not Domain.CORE:
            raise ValueError("the context review desk runs in core")
        self._mailbox = mailbox
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._items: dict[str, ContextReview] = {}
        if path is not None and path.exists():
            self._items = {r.request.brief_id: r for r in _REVIEWS.validate_json(path.read_bytes())}

    def sync(self) -> None:
        """Take in the review requests content sent since the last call."""
        with self._lock:
            changed = False
            for letter in self._mailbox.receive(Domain.CONTENT):
                event = letter.event
                if isinstance(event, ContextReviewRequested) and (
                    event.brief_id not in self._items
                ):
                    self._items[event.brief_id] = ContextReview(
                        request=event, received_at=self._clock()
                    )
                    changed = True
                self._mailbox.ack(letter)
            if changed:
                self._save()

    def decide(self, brief_id: str, accept: bool, by: str, note: str = "") -> ContextReview:
        with self._lock:
            review = self._items.get(brief_id)
            if review is None or review.decided_at is not None:
                raise ReviewUnknown(brief_id)
            self._mailbox.send(
                ContextReviewDecided(
                    origin_domain=Domain.CORE,
                    target_domain=Domain.CONTENT,
                    classification=DataClassification.INTERNAL,
                    brief_id=brief_id,
                    accept=accept,
                    decided_by=by,
                    note=note,
                )
            )
            decided = review.model_copy(
                update={"decided_at": self._clock(), "accepted": accept, "decided_by": by}
            )
            self._items[brief_id] = decided
            self._prune()
            self._save()
            return decided

    def open(self) -> list[ContextReview]:
        with self._lock:
            pending = [r for r in self._items.values() if r.decided_at is None]
        return sorted(pending, key=lambda r: r.received_at)

    def recent(self) -> list[ContextReview]:
        with self._lock:
            done = [r for r in self._items.values() if r.decided_at is not None]
        return sorted(done, key=lambda r: r.decided_at or r.received_at, reverse=True)

    def _prune(self) -> None:
        done = sorted(
            (r for r in self._items.values() if r.decided_at is not None),
            key=lambda r: r.decided_at or r.received_at,
            reverse=True,
        )
        for old in done[MAX_KEPT_DECIDED:]:
            del self._items[old.request.brief_id]

    def _save(self) -> None:
        if self._path is not None:
            write_atomic(self._path, _REVIEWS.dump_json(list(self._items.values())))
