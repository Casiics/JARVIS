"""Core's orchestrator service: owns every cross-domain task from goal to final state.

Core plans and tracks; domains do the work. The orchestrator never calls a model and never
reads a domain's data (docs/architecture.md, 3.1). It talks to domains only through the
boundary mailboxes:

- `submit()` routes a typed goal to the one domain that may do it (the task router), sends
  the single boundary event the flow matrix allows for it, and records a task in PLANNED.
- `sync()` reads the metadata-only `TaskMetadata` reports every domain sends back and moves
  the task's core-side record. Reports for tasks core never issued, from a domain other than
  the one the task went to, or after a final state are ignored and audited as denials.
- `expire()` escalates tasks whose domain stayed silent past their deadline (restarted by
  every report), so a stopped worker shows up in the deck instead of hanging forever.

Every change is audited (hashes and references only) and published as telemetry for the
deck's live view. Tasks persist in one JSON file; production moves them into pg-core behind
the same interface.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from jarvis_core.boundary.mailbox import Mailbox
from jarvis_core.contracts.audit import AuditAction, AuditDecision, AuditLogEntry
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import BoundaryEvent, ReelSubmitted, TaskMetadata
from jarvis_core.contracts.reel_url import canonical_reel_url
from jarvis_core.contracts.telemetry import TelemetryEvent, TelemetryState
from jarvis_core.orchestrator.state_machine import TaskState
from jarvis_core.storage import write_atomic

ACTOR = "spiffe://jarvis.home/core/orchestrator"
# Silence allowed between two signs of life from the domain. Reels queue behind each other in
# one worker, so the deadline restarts with every report instead of counting from submission.
DEFAULT_DEADLINE = timedelta(hours=2)

AuditSink = Callable[[AuditLogEntry], object]
TelemetrySink = Callable[[TelemetryEvent], object]

_FINAL = frozenset({TaskState.DONE, TaskState.ESCALATED})
# Domains report the state names of their own TaskMachine. AWAITING_ACTION is final for core:
# the approval runs through the deck and the domain's executor, not through the orchestrator.
_SETTLED = _FINAL | {TaskState.AWAITING_ACTION}


# --- goals: what the owner (or a schedule) asks for, typed per kind ------------------------


class ReelGoal(BaseModel):
    """Process one Instagram reel in the content domain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["reel"] = "reel"
    source_ref: str = Field(max_length=200)

    @field_validator("source_ref")
    @classmethod
    def _canonical(cls, value: str) -> str:
        return canonical_reel_url(value)


Goal = Annotated[ReelGoal, Field(discriminator="kind")]


class Dispatcher(Protocol):
    """Turns a goal into the boundary event its domain accepts."""

    domain: Domain

    def event(self, goal: Goal, task_id: UUID) -> BoundaryEvent: ...


class ReelDispatcher:
    domain = Domain.CONTENT

    def event(self, goal: Goal, task_id: UUID) -> BoundaryEvent:
        return ReelSubmitted(
            origin_domain=Domain.CORE,
            target_domain=Domain.CONTENT,
            classification=DataClassification.INTERNAL,
            task_id=task_id,
            source_ref=goal.source_ref,
        )


# The task router: one goal kind, one domain. New kinds need a boundary event that
# core/jarvis_core/policy/flows.py allows from core to that domain (a human change).
ROUTES: dict[str, Dispatcher] = {"reel": ReelDispatcher()}


class UnroutableGoal(Exception):
    pass


# --- task records ---------------------------------------------------------------------------


class TaskRecord(BaseModel):
    """Core's view of one task. Metadata only: no prompts, outputs or domain data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID
    kind: str = Field(max_length=40)
    domain: Domain
    ref_hash: str = Field(pattern=r"^[0-9a-f]{64}$", description="SHA-256 of the goal")
    state: TaskState = TaskState.PLANNED
    created_at: datetime
    updated_at: datetime
    deadline: datetime
    duration_ms: int | None = Field(default=None, ge=0)
    error_class: str | None = Field(default=None, max_length=80)
    reports: int = Field(default=0, ge=0)

    @property
    def settled(self) -> bool:
        return self.state in _SETTLED


_RECORDS = TypeAdapter(list[TaskRecord])


def goal_hash(goal: Goal) -> str:
    return hashlib.sha256(goal.model_dump_json().encode()).hexdigest()


def _task_state(reported: str) -> TaskState:
    try:
        return TaskState(reported)
    except ValueError:
        return TaskState.RUNNING


class Orchestrator:
    def __init__(
        self,
        mailbox: Mailbox,
        path: Path | None,
        *,
        clock: Callable[[], datetime],
        audit: AuditSink = lambda _e: None,
        telemetry: TelemetrySink = lambda _e: None,
        deadline: timedelta = DEFAULT_DEADLINE,
        routes: dict[str, Dispatcher] | None = None,
    ) -> None:
        if mailbox.me is not Domain.CORE:
            raise ValueError("the orchestrator runs in core")
        self._mailbox = mailbox
        self._path = path
        self._clock = clock
        self._audit = audit
        self._telemetry = telemetry
        self._deadline = deadline
        self._routes = ROUTES if routes is None else routes
        self._lock = threading.RLock()
        self._tasks: dict[UUID, TaskRecord] = {}
        if path is not None and path.exists():
            self._tasks = {t.task_id: t for t in _RECORDS.validate_json(path.read_bytes())}

    # --- public API ------------------------------------------------------------------------

    def submit(self, goal: Goal, *, actor: str = ACTOR) -> TaskRecord:
        """Route a goal to its domain and send the one event that starts it there."""
        dispatcher = self._routes.get(goal.kind)
        if dispatcher is None:
            raise UnroutableGoal(goal.kind)
        task_id = uuid4()
        event = dispatcher.event(goal, task_id)
        now = self._clock()
        record = TaskRecord(
            task_id=task_id,
            kind=goal.kind,
            domain=dispatcher.domain,
            ref_hash=goal_hash(goal),
            created_at=now,
            updated_at=now,
            deadline=now + self._deadline,
        )
        with self._lock:
            self._mailbox.send(event)  # refused by the flow policy -> raises, nothing recorded
            self._tasks[task_id] = record
            self._save()
        self._audit_entry(
            record,
            f"dispatch:{goal.kind}->{dispatcher.domain.value}",
            AuditDecision.ALLOW,
            actor=actor,
            action=AuditAction.BOUNDARY_EVENT,
            content_hash=record.ref_hash,
        )
        self._emit(record, "orchestrator:dispatch")
        return record

    def adopt(self, task_id: UUID, goal: Goal, created_at: datetime) -> TaskRecord:
        """Track a task that was dispatched before the orchestrator existed (or knew of it).

        Sends nothing: the domain already has the task. Without this, reports for such tasks
        would be ignored as unknown and the task would look open forever.
        """
        dispatcher = self._routes.get(goal.kind)
        if dispatcher is None:
            raise UnroutableGoal(goal.kind)
        now = self._clock()
        with self._lock:
            known = self._tasks.get(task_id)
            if known is not None:
                return known
            record = TaskRecord(
                task_id=task_id,
                kind=goal.kind,
                domain=dispatcher.domain,
                ref_hash=goal_hash(goal),
                created_at=created_at,
                updated_at=now,
                deadline=now + self._deadline,
            )
            self._tasks[task_id] = record
            self._save()
        self._audit_entry(
            record, f"adopt:{goal.kind}->{dispatcher.domain.value}", AuditDecision.ALLOW
        )
        return record

    def sync(self) -> list[TaskRecord]:
        """Apply every status report the domains sent since the last call."""
        changed: list[TaskRecord] = []
        with self._lock:
            for origin in Domain:
                if origin is Domain.CORE:
                    continue
                for letter in self._mailbox.receive(origin):
                    event = letter.event
                    if isinstance(event, TaskMetadata):
                        updated = self._apply(origin, event)
                        if updated is not None:
                            changed.append(updated)
                    self._mailbox.ack(letter)
            if changed:
                self._save()
        return changed

    def expire(self) -> list[TaskRecord]:
        """Escalate open tasks whose domain stayed silent past their deadline."""
        now = self._clock()
        expired: list[TaskRecord] = []
        with self._lock:
            for record in list(self._tasks.values()):
                if record.settled or record.deadline > now:
                    continue
                moved = record.model_copy(
                    update={
                        "state": TaskState.ESCALATED,
                        "updated_at": now,
                        "error_class": "timeout",
                    }
                )
                self._tasks[record.task_id] = moved
                expired.append(moved)
            if expired:
                self._save()
        for record in expired:
            self._audit_entry(record, f"{record.domain.value}:timeout", AuditDecision.ESCALATE)
            self._emit(record, "orchestrator:timeout")
        return expired

    def tick(self) -> list[TaskRecord]:
        """One pass of the service loop: reports first, then deadlines."""
        return self.sync() + self.expire()

    def get(self, task_id: UUID) -> TaskRecord | None:
        with self._lock:
            return self._tasks.get(task_id)

    def tasks(self) -> list[TaskRecord]:
        with self._lock:
            return sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)

    # --- internals -------------------------------------------------------------------------

    def _apply(self, origin: Domain, event: TaskMetadata) -> TaskRecord | None:
        record = self._tasks.get(event.task_id)
        if record is None or record.domain is not origin:
            self._audit(
                AuditLogEntry(
                    trace_id=event.task_id.hex,
                    task_id=event.task_id,
                    domain=Domain.CORE,
                    actor=ACTOR,
                    action=AuditAction.BOUNDARY_EVENT,
                    target=f"report_ignored:{origin.value}:unknown_task",
                    decision=AuditDecision.DENY,
                )
            )
            return None
        if record.settled:
            # A late "running" after "done" must not reopen a task.
            return None
        state = _task_state(event.state)
        moved = record.model_copy(
            update={
                "state": state,
                "updated_at": event.created_at,
                "duration_ms": event.duration_ms,
                "error_class": event.error_class,
                "reports": record.reports + 1,
                "deadline": max(record.deadline, self._clock() + self._deadline),
            }
        )
        self._tasks[record.task_id] = moved
        decision = AuditDecision.ESCALATE if state is TaskState.ESCALATED else AuditDecision.ALLOW
        self._audit_entry(moved, f"{origin.value}:report:{state.value}", decision)
        self._emit(moved, f"{origin.value}:{moved.kind}")
        return moved

    def _audit_entry(
        self,
        record: TaskRecord,
        target: str,
        decision: AuditDecision,
        *,
        actor: str = ACTOR,
        action: AuditAction = AuditAction.STATE_TRANSITION,
        content_hash: str | None = None,
    ) -> None:
        self._audit(
            AuditLogEntry(
                trace_id=record.task_id.hex,
                task_id=record.task_id,
                domain=Domain.CORE,
                actor=actor,
                action=action,
                target=target[:200],
                decision=decision,
                content_hash=content_hash,
            )
        )

    def _emit(self, record: TaskRecord, node: str) -> None:
        self._telemetry(
            TelemetryEvent(
                ts=record.updated_at,
                task_id=record.task_id,
                domain=record.domain,
                node=node,
                state=TelemetryState(record.state.value),
                duration_ms=record.duration_ms or 0,
                error_class=_safe_error(record.error_class),
            )
        )

    def _save(self) -> None:
        if self._path is not None:
            write_atomic(self._path, _RECORDS.dump_json(list(self._tasks.values())))


def _safe_error(error_class: str | None) -> str | None:
    """Telemetry error classes are lower-case slugs; anything else is reported generically."""
    if error_class is None:
        return None
    slug = error_class.strip().lower()
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789_.:-")
    if slug and len(slug) <= 80 and set(slug) <= allowed:
        return slug
    return "domain_error"


def run_forever(
    orchestrator: Orchestrator,
    interval_s: float,
    *,
    stop: threading.Event | None = None,
    on_error: Callable[[Exception], object] = lambda _e: None,
) -> None:
    """The service loop: tick until stopped. One bad pass never stops the loop."""
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            orchestrator.tick()
        except Exception as exc:  # keep serving; the error is reported, not swallowed silently
            on_error(exc)
        stop.wait(interval_s)
