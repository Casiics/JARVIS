import pytest

from jarvis_core.contracts.audit import AuditLogEntry
from jarvis_core.contracts.classification import Domain
from jarvis_core.orchestrator.state_machine import (
    MAX_ITERATIONS,
    Event,
    InvalidTransition,
    Task,
    TaskMachine,
    TaskState,
)


def _machine() -> tuple[TaskMachine, list[AuditLogEntry]]:
    log: list[AuditLogEntry] = []
    return TaskMachine(log.append, actor="spiffe://jarvis.home/core/orchestrator"), log


def test_happy_path_with_action_is_fully_audited() -> None:
    machine, log = _machine()
    task = Task(domain=Domain.CONTENT, trace_id="t1")
    for event in [
        Event.START,
        Event.WORKER_FINISHED,
        Event.GATE_PASSED,
        Event.QA_ACCEPTED_WITH_ACTION,
        Event.ACTION_APPROVED,
        Event.EXECUTED,
    ]:
        task = machine.apply(task, event)
    assert task.state is TaskState.DONE
    assert len(log) == 6


def test_gate_failure_loops_back_without_supervisor() -> None:
    machine, _ = _machine()
    task = machine.apply(Task(domain=Domain.TRADING, trace_id="t"), Event.START)
    task = machine.apply(task, Event.WORKER_FINISHED)
    task = machine.apply(task, Event.GATE_FAILED)
    assert task.state is TaskState.RUNNING


def test_escalates_after_max_iterations() -> None:
    machine, _ = _machine()
    task = machine.apply(Task(domain=Domain.TRADING, trace_id="t"), Event.START)
    for _ in range(MAX_ITERATIONS + 1):
        task = machine.apply(task, Event.WORKER_FINISHED)
        task = machine.apply(task, Event.GATE_FAILED)
        if task.state is TaskState.ESCALATED:
            break
    assert task.state is TaskState.ESCALATED
    assert task.iterations == MAX_ITERATIONS


def test_budget_exhaustion_escalates_from_any_state() -> None:
    machine, _ = _machine()
    task = machine.apply(Task(domain=Domain.CORE, trace_id="t"), Event.START)
    assert machine.apply(task, Event.BUDGET_EXHAUSTED).state is TaskState.ESCALATED


def test_terminal_states_accept_no_events() -> None:
    machine, _ = _machine()
    task = machine.apply(Task(domain=Domain.CORE, trace_id="t"), Event.BUDGET_EXHAUSTED)
    with pytest.raises(InvalidTransition):
        machine.apply(task, Event.START)


def test_invalid_event_is_rejected() -> None:
    machine, _ = _machine()
    with pytest.raises(InvalidTransition):
        machine.apply(Task(domain=Domain.CORE, trace_id="t"), Event.EXECUTED)


def test_failed_deterministic_step_escalates_and_is_audited() -> None:
    machine, log = _machine()
    task = machine.apply(Task(domain=Domain.CONTENT, trace_id="t"), Event.START)
    task = machine.apply(task, Event.STEP_FAILED)
    assert task.state is TaskState.ESCALATED
    assert log[-1].decision.value == "escalate"
