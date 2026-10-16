"""Reel intake from the deck: the owner submits a reel link, content processes it.

Core never touches content's data. It sends a ReelSubmitted event through the core-to-content
mailbox and learns progress only from TaskMetadata that content sends back (state, duration,
error class). What core keeps is its own record of what the owner submitted.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from jarvis_core.api.filestore import write_atomic
from jarvis_core.boundary.mailbox import Mailbox
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import ReelSubmitted, TaskMetadata
from jarvis_core.contracts.reel_url import canonical_reel_url

MAX_OPEN = 20


class ReelStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    AWAITING_ACTION = "awaiting_action"
    FAILED = "failed"


_FROM_TASK_STATE = {
    "done": ReelStatus.DONE,
    "awaiting_action": ReelStatus.AWAITING_ACTION,
    "escalated": ReelStatus.FAILED,
}
_OPEN = frozenset({ReelStatus.QUEUED, ReelStatus.RUNNING})


class ReelSubmission(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID
    source_ref: str
    submitted_at: datetime
    status: ReelStatus = ReelStatus.QUEUED
    updated_at: datetime
    duration_ms: int | None = Field(default=None, ge=0)
    error_class: str | None = None

    @property
    def reel_code(self) -> str:
        return self.source_ref.rstrip("/").rsplit("/", 1)[-1]


_SUBMISSIONS = TypeAdapter(list[ReelSubmission])


class TooManyOpenReels(Exception):
    pass


class ReelDesk:
    def __init__(
        self,
        mailbox: Mailbox,
        path: Path | None,
        clock: Callable[[], datetime],
    ) -> None:
        if mailbox.me is not Domain.CORE:
            raise ValueError("the reel desk runs in core")
        self._mailbox = mailbox
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._items: dict[UUID, ReelSubmission] = {}
        if path is not None and path.exists():
            self._items = {s.task_id: s for s in _SUBMISSIONS.validate_json(path.read_bytes())}

    def submit(self, url: str) -> tuple[ReelSubmission, bool]:
        """Queue a reel. Returns the submission and whether it is new.

        Raises InvalidReelUrl for anything but an Instagram reel link.
        """
        source_ref = canonical_reel_url(url)
        with self._lock:
            for existing in self._items.values():
                if existing.source_ref == source_ref and existing.status is not ReelStatus.FAILED:
                    return existing, False
            if sum(s.status in _OPEN for s in self._items.values()) >= MAX_OPEN:
                raise TooManyOpenReels
            now = self._clock()
            submission = ReelSubmission(
                task_id=uuid4(), source_ref=source_ref, submitted_at=now, updated_at=now
            )
            self._mailbox.send(
                ReelSubmitted(
                    origin_domain=Domain.CORE,
                    target_domain=Domain.CONTENT,
                    classification=DataClassification.INTERNAL,
                    task_id=submission.task_id,
                    source_ref=source_ref,
                )
            )
            self._items[submission.task_id] = submission
            self._save()
            return submission, True

    def sync(self) -> None:
        """Apply the status reports content sent back since the last call."""
        with self._lock:
            changed = False
            for letter in self._mailbox.receive(Domain.CONTENT):
                event = letter.event
                if isinstance(event, TaskMetadata) and event.task_id in self._items:
                    self._items[event.task_id] = self._items[event.task_id].model_copy(
                        update={
                            "status": _FROM_TASK_STATE.get(event.state, ReelStatus.RUNNING),
                            "updated_at": event.created_at,
                            "duration_ms": event.duration_ms,
                            "error_class": event.error_class,
                        }
                    )
                    changed = True
                self._mailbox.ack(letter)
            if changed:
                self._save()

    def all(self) -> list[ReelSubmission]:
        with self._lock:
            return sorted(self._items.values(), key=lambda s: s.submitted_at, reverse=True)

    def _save(self) -> None:
        if self._path is not None:
            write_atomic(self._path, _SUBMISSIONS.dump_json(list(self._items.values())))
