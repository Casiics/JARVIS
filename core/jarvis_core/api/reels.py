"""Reel intake from the deck: the owner submits a reel link, content processes it.

Core never touches content's data. The desk hands each reel to core's orchestrator, which
sends the ReelSubmitted event through the core-to-content mailbox and learns progress only from
the TaskMetadata content sends back (state, duration, error class). What the desk keeps is its
own record of what the owner submitted; the task's state comes from the orchestrator.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from jarvis_core.contracts.reel_url import canonical_reel_url
from jarvis_core.orchestrator.service import Orchestrator, ReelGoal, TaskRecord
from jarvis_core.orchestrator.state_machine import TaskState
from jarvis_core.storage import write_atomic

MAX_OPEN = 20


class ReelStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    AWAITING_ACTION = "awaiting_action"
    FAILED = "failed"


_FROM_TASK_STATE = {
    TaskState.PLANNED: ReelStatus.QUEUED,
    TaskState.DONE: ReelStatus.DONE,
    TaskState.AWAITING_ACTION: ReelStatus.AWAITING_ACTION,
    TaskState.ESCALATED: ReelStatus.FAILED,
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
        orchestrator: Orchestrator,
        path: Path | None,
        clock: Callable[[], datetime],
    ) -> None:
        self._orchestrator = orchestrator
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._items: dict[UUID, ReelSubmission] = {}
        if path is not None and path.exists():
            self._items = {s.task_id: s for s in _SUBMISSIONS.validate_json(path.read_bytes())}
        # Reels queued before the orchestrator took over intake: let it track them, so their
        # reports still arrive and a silent worker still escalates them.
        for submission in self._items.values():
            if submission.status in _OPEN and orchestrator.get(submission.task_id) is None:
                orchestrator.adopt(
                    submission.task_id,
                    ReelGoal(source_ref=submission.source_ref),
                    submission.submitted_at,
                )

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
            record = self._orchestrator.submit(ReelGoal(source_ref=source_ref))
            submission = ReelSubmission(
                task_id=record.task_id,
                source_ref=source_ref,
                submitted_at=record.created_at,
                updated_at=record.created_at,
            )
            self._items[submission.task_id] = submission
            self._save()
            return submission, True

    def sync(self) -> None:
        """Pull the latest task states from the orchestrator."""
        self._orchestrator.tick()
        with self._lock:
            changed = False
            for task_id, submission in list(self._items.items()):
                record = self._orchestrator.get(task_id)
                if record is None or record.updated_at == submission.updated_at:
                    continue
                self._items[task_id] = _merge(submission, record)
                changed = True
            if changed:
                self._save()

    def all(self) -> list[ReelSubmission]:
        with self._lock:
            return sorted(self._items.values(), key=lambda s: s.submitted_at, reverse=True)

    def _save(self) -> None:
        if self._path is not None:
            write_atomic(self._path, _SUBMISSIONS.dump_json(list(self._items.values())))


def _merge(submission: ReelSubmission, record: TaskRecord) -> ReelSubmission:
    return submission.model_copy(
        update={
            "status": _FROM_TASK_STATE.get(record.state, ReelStatus.RUNNING),
            "updated_at": record.updated_at,
            "duration_ms": record.duration_ms,
            "error_class": record.error_class,
        }
    )
