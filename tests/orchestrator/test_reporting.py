"""A domain's loop reports each stage to core; the orchestrator follows along."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from jarvis_core.boundary.mailbox import Mailbox
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import TaskMetadata
from jarvis_core.orchestrator.loop import (
    Acceptance,
    RuntimeSupervisor,
    RuntimeWorker,
    SupervisorWorkerLoop,
    WorkOrder,
)
from jarvis_core.orchestrator.reporting import MetadataReporter
from jarvis_core.orchestrator.service import Orchestrator, ReelGoal
from jarvis_core.orchestrator.state_machine import TaskMachine, TaskState
from tests.orchestrator.fakes import ScriptedRuntime, claims
from tests.orchestrator.test_loop import GOOD, OK, Summary

REEL = "https://www.instagram.com/reel/ABCDE12345/"


def test_each_stage_reaches_the_orchestrator_once(tmp_path: Path) -> None:
    orchestrator = Orchestrator(
        Mailbox(tmp_path, Domain.CORE), None, clock=lambda: datetime.now(UTC)
    )
    record = orchestrator.submit(ReelGoal(source_ref=REEL))
    reporter = MetadataReporter(Mailbox(tmp_path, Domain.CONTENT))
    runtime = ScriptedRuntime(Domain.CONTENT, worker=["bad", GOOD], supervisor=[OK])
    order_claims = claims(Domain.CONTENT).model_copy(update={"task_id": record.task_id})
    loop = SupervisorWorkerLoop(TaskMachine(lambda _e: None, "content/loop"), reporter.sink)

    outcome = loop.run(
        WorkOrder(
            claims=order_claims,
            trace_id="t",
            instruction="Summarise.",
            label=DataClassification.PUBLIC,
        ),
        Acceptance(Summary),
        RuntimeWorker(runtime),
        RuntimeSupervisor(runtime),
    )
    assert outcome.state is TaskState.DONE

    sent = [
        letter.event
        for letter in Mailbox(tmp_path, Domain.CORE).receive(Domain.CONTENT)
        if isinstance(letter.event, TaskMetadata)
    ]
    # running, gate_check, running (gate failed), gate_check, supervisor_qa, done
    assert [e.state for e in sent] == [
        "running",
        "gate_check",
        "running",
        "gate_check",
        "supervisor_qa",
        "done",
    ]
    changed = orchestrator.sync()
    assert changed[-1].state is TaskState.DONE
    assert TaskState.SUPERVISOR_QA in {c.state for c in changed}


def test_core_cannot_use_the_reporter(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="core"):
        MetadataReporter(Mailbox(tmp_path, Domain.CORE))
