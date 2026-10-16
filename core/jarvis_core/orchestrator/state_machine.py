"""Supervisor-worker loop as a state machine (docs/architecture.md, section 4).

PLANNED -> RUNNING -> GATE_CHECK --fail--> RUNNING (error trace, no supervisor call)
                                 --pass--> SUPERVISOR_QA --reject--> RUNNING (feedback)
                                                         --accept--> DONE | AWAITING_ACTION
AWAITING_ACTION --approved--> EXECUTING -> DONE ; --rejected--> ESCALATED
Any retry beyond MAX_ITERATIONS, or an exhausted budget -> ESCALATED.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from jarvis_core.contracts.audit import AuditAction, AuditDecision, AuditLogEntry
from jarvis_core.contracts.classification import Domain

MAX_ITERATIONS = 4


class TaskState(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    GATE_CHECK = "gate_check"
    SUPERVISOR_QA = "supervisor_qa"
    AWAITING_ACTION = "awaiting_action"
    EXECUTING = "executing"
    DONE = "done"
    ESCALATED = "escalated"


class Event(StrEnum):
    START = "start"
    WORKER_FINISHED = "worker_finished"
    GATE_PASSED = "gate_passed"
    GATE_FAILED = "gate_failed"
    QA_ACCEPTED = "qa_accepted"
    QA_ACCEPTED_WITH_ACTION = "qa_accepted_with_action"
    QA_REJECTED = "qa_rejected"
    ACTION_APPROVED = "action_approved"
    ACTION_REJECTED = "action_rejected"
    EXECUTED = "executed"
    BUDGET_EXHAUSTED = "budget_exhausted"


_TRANSITIONS: dict[tuple[TaskState, Event], TaskState] = {
    (TaskState.PLANNED, Event.START): TaskState.RUNNING,
    (TaskState.RUNNING, Event.WORKER_FINISHED): TaskState.GATE_CHECK,
    (TaskState.GATE_CHECK, Event.GATE_FAILED): TaskState.RUNNING,
    (TaskState.GATE_CHECK, Event.GATE_PASSED): TaskState.SUPERVISOR_QA,
    (TaskState.SUPERVISOR_QA, Event.QA_REJECTED): TaskState.RUNNING,
    (TaskState.SUPERVISOR_QA, Event.QA_ACCEPTED): TaskState.DONE,
    (TaskState.SUPERVISOR_QA, Event.QA_ACCEPTED_WITH_ACTION): TaskState.AWAITING_ACTION,
    (TaskState.AWAITING_ACTION, Event.ACTION_APPROVED): TaskState.EXECUTING,
    (TaskState.AWAITING_ACTION, Event.ACTION_REJECTED): TaskState.ESCALATED,
    (TaskState.EXECUTING, Event.EXECUTED): TaskState.DONE,
}

_RETRY_EVENTS = frozenset({Event.GATE_FAILED, Event.QA_REJECTED})
_TERMINAL = frozenset({TaskState.DONE, TaskState.ESCALATED})


class InvalidTransition(Exception):
    pass


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID = Field(default_factory=uuid4)
    domain: Domain
    trace_id: str
    state: TaskState = TaskState.PLANNED
    iterations: int = 0


AuditSink = Callable[[AuditLogEntry], object]


class TaskMachine:
    """Applies events to tasks and audits every transition."""

    def __init__(self, audit: AuditSink, actor: str) -> None:
        self._audit = audit
        self._actor = actor

    def apply(self, task: Task, event: Event) -> Task:
        if task.state in _TERMINAL:
            raise InvalidTransition(f"task is {task.state}; no further events")

        if event is Event.BUDGET_EXHAUSTED:
            return self._move(task, TaskState.ESCALATED, event, AuditDecision.ESCALATE)

        target = _TRANSITIONS.get((task.state, event))
        if target is None:
            raise InvalidTransition(f"{event} is not valid in state {task.state}")

        if event in _RETRY_EVENTS:
            if task.iterations >= MAX_ITERATIONS:
                return self._move(task, TaskState.ESCALATED, event, AuditDecision.ESCALATE)
            decision = AuditDecision.DENY
        elif event is Event.ACTION_REJECTED:
            decision = AuditDecision.ESCALATE
        else:
            decision = AuditDecision.ALLOW

        moved = self._move(task, target, event, decision)
        if event is Event.START or event in _RETRY_EVENTS:
            moved = moved.model_copy(update={"iterations": moved.iterations + 1})
        return moved

    def _move(self, task: Task, target: TaskState, event: Event, decision: AuditDecision) -> Task:
        self._audit(
            AuditLogEntry(
                trace_id=task.trace_id,
                task_id=task.task_id,
                domain=task.domain,
                actor=self._actor,
                action=AuditAction.STATE_TRANSITION,
                target=f"{task.state}->{target} on {event}",
                decision=decision,
            )
        )
        return task.model_copy(update={"state": target})
