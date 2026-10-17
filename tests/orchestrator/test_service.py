"""Core's orchestrator: routes goals to domains and tracks them by metadata only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jarvis_core.boundary.mailbox import Mailbox, MailboxError
from jarvis_core.contracts.audit import AuditDecision, AuditLogEntry
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import BoundaryEvent, ReelSubmitted, Taint, TaskMetadata
from jarvis_core.contracts.task_token import Ed25519TokenVerifier, public_key_hex
from jarvis_core.contracts.telemetry import TelemetryEvent, TelemetryState
from jarvis_core.orchestrator.service import (
    DEFAULT_DEADLINE,
    Goal,
    Orchestrator,
    ReelDispatcher,
    ReelGoal,
    TaskRecord,
    TaskTokenIssuer,
    UnroutableGoal,
)
from jarvis_core.orchestrator.state_machine import TaskState
from mod_content.reel import __main__ as content_cli
from mod_content.reel.pipeline import REEL_TOOLS

REEL = "https://www.instagram.com/reel/ABCDE12345/"
CORE_KEY = Ed25519PrivateKey.from_private_bytes(b"c" * 32)
VERIFIER = Ed25519TokenVerifier(CORE_KEY.public_key())


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Harness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.clock = Clock()
        self.audit: list[AuditLogEntry] = []
        self.telemetry: list[TelemetryEvent] = []
        self.orchestrator = self.build()

    def build(self) -> Orchestrator:
        return Orchestrator(
            Mailbox(self.root / "boundary", Domain.CORE),
            self.root / "core/tasks.json",
            clock=self.clock,
            audit=self.audit.append,
            telemetry=self.telemetry.append,
            deadline=timedelta(minutes=10),
            # The content worker checks expiry against the wall clock, not this test clock.
            tokens=TaskTokenIssuer(CORE_KEY, ttl=timedelta(days=3650)),
        )

    def mailbox(self, domain: Domain) -> Mailbox:
        return Mailbox(self.root / "boundary", domain)

    def report(
        self,
        task_id: UUID,
        state: str,
        *,
        sender: Domain = Domain.CONTENT,
        error: str | None = None,
    ) -> None:
        self.mailbox(sender).send(
            TaskMetadata(
                origin_domain=sender,
                target_domain=Domain.CORE,
                classification=DataClassification.INTERNAL,
                task_id=task_id,
                state=state,
                duration_ms=1500,
                error_class=error,
            )
        )


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_a_reel_goal_goes_to_content_as_its_one_allowed_event(h: Harness) -> None:
    record = h.orchestrator.submit(ReelGoal(source_ref=REEL + "?igsh=tracking"))

    assert record.domain is Domain.CONTENT and record.state is TaskState.PLANNED
    [letter] = h.mailbox(Domain.CONTENT).receive(Domain.CORE)
    assert isinstance(letter.event, ReelSubmitted)
    assert letter.event.task_id == record.task_id and letter.event.source_ref == REEL
    # Core keeps a hash of the goal, never the goal itself.
    assert REEL not in record.model_dump_json()
    assert all(REEL not in e.model_dump_json() for e in h.audit)
    assert [t.state for t in h.telemetry] == [TelemetryState.PLANNED]


def test_reports_move_the_task_and_reach_the_deck_as_telemetry(h: Harness) -> None:
    record = h.orchestrator.submit(ReelGoal(source_ref=REEL))
    h.report(record.task_id, "running")
    h.report(record.task_id, "done")

    changed = h.orchestrator.sync()

    assert [r.state for r in changed] == [TaskState.RUNNING, TaskState.DONE]
    task = h.orchestrator.get(record.task_id)
    assert task is not None and task.state is TaskState.DONE and task.reports == 2
    assert [t.state for t in h.telemetry] == [
        TelemetryState.PLANNED,
        TelemetryState.RUNNING,
        TelemetryState.DONE,
    ]
    assert h.telemetry[-1].node == "content:reel"


def test_a_settled_task_is_not_reopened_by_a_late_report(h: Harness) -> None:
    record = h.orchestrator.submit(ReelGoal(source_ref=REEL))
    h.report(record.task_id, "escalated", error="fetch_failed")
    h.orchestrator.sync()
    h.report(record.task_id, "running")
    assert h.orchestrator.sync() == []
    task = h.orchestrator.get(record.task_id)
    assert task is not None and task.state is TaskState.ESCALATED
    assert task.error_class == "fetch_failed"


@pytest.mark.parametrize("sender", [Domain.TRADING, Domain.PRIVATE])
def test_reports_from_the_wrong_domain_are_ignored_and_audited(h: Harness, sender: Domain) -> None:
    record = h.orchestrator.submit(ReelGoal(source_ref=REEL))
    h.report(record.task_id, "done", sender=sender)
    h.report(uuid4(), "done")

    assert h.orchestrator.sync() == []
    task = h.orchestrator.get(record.task_id)
    assert task is not None and task.state is TaskState.PLANNED
    denied = [e for e in h.audit if e.decision is AuditDecision.DENY]
    assert len(denied) == 2
    # Ignored reports are consumed, not retried forever.
    assert h.mailbox(Domain.CORE).receive(sender) == []


def test_unknown_reported_states_count_as_running(h: Harness) -> None:
    record = h.orchestrator.submit(ReelGoal(source_ref=REEL))
    h.report(record.task_id, "gate_check-ish")
    [moved] = h.orchestrator.sync()
    assert moved.state is TaskState.RUNNING


def test_silent_domains_are_escalated_after_the_deadline(h: Harness) -> None:
    record = h.orchestrator.submit(ReelGoal(source_ref=REEL))
    h.clock.now += timedelta(minutes=9)
    h.report(record.task_id, "running")
    h.orchestrator.sync()
    # The report restarted the deadline.
    h.clock.now += timedelta(minutes=9)
    assert h.orchestrator.expire() == []

    h.clock.now += timedelta(minutes=2)
    [expired] = h.orchestrator.tick()
    assert expired.state is TaskState.ESCALATED and expired.error_class == "timeout"
    assert h.telemetry[-1].node == "orchestrator:timeout"
    assert h.audit[-1].decision is AuditDecision.ESCALATE


def test_tasks_survive_a_restart(h: Harness) -> None:
    record = h.orchestrator.submit(ReelGoal(source_ref=REEL))
    again = h.build()
    assert [t.task_id for t in again.tasks()] == [record.task_id]
    h.report(record.task_id, "done")
    [moved] = again.sync()
    assert moved.state is TaskState.DONE


def test_goals_without_a_route_are_refused(h: Harness) -> None:
    orchestrator = Orchestrator(h.mailbox(Domain.CORE), None, clock=h.clock, routes={})
    with pytest.raises(UnroutableGoal):
        orchestrator.submit(ReelGoal(source_ref=REEL))
    assert orchestrator.tasks() == []


def test_a_dispatch_the_flow_policy_refuses_records_nothing(h: Harness) -> None:
    class ToTrading:
        domain = Domain.TRADING
        tools = frozenset({"sandbox.fetch"})
        taint = Taint.NONE

        def event(self, goal: Goal, task_id: UUID, token: str | None) -> BoundaryEvent:
            assert isinstance(goal, ReelGoal)
            return ReelSubmitted(
                origin_domain=Domain.CORE,
                target_domain=Domain.TRADING,
                classification=DataClassification.INTERNAL,
                task_id=task_id,
                source_ref=goal.source_ref,
            )

    orchestrator = Orchestrator(
        h.mailbox(Domain.CORE), None, clock=h.clock, routes={"reel": ToTrading()}
    )
    with pytest.raises(MailboxError):
        orchestrator.submit(ReelGoal(source_ref=REEL))
    assert orchestrator.tasks() == []


def test_only_core_runs_the_orchestrator(h: Harness) -> None:
    with pytest.raises(ValueError, match="core"):
        Orchestrator(h.mailbox(Domain.CONTENT), None, clock=h.clock)


def test_error_classes_are_sanitised_for_telemetry(h: Harness) -> None:
    record = h.orchestrator.submit(ReelGoal(source_ref=REEL))
    h.report(record.task_id, "escalated", error="Fetch Failed: <script>")
    h.orchestrator.sync()
    assert h.telemetry[-1].error_class == "domain_error"


# --- end to end: core -> content's real serve loop -> core -----------------------------------


class FinishingPipeline:
    """Stands in for the sandbox and models; reports the final state like the real pipeline."""

    def __init__(self, mailbox: Mailbox) -> None:
        self.mailbox = mailbox
        self.urls: list[str] = []

    def run(self, url: str, token: str) -> object:
        self.urls.append(url)
        task_id = VERIFIER.verify(token).task_id
        self.mailbox.send(
            TaskMetadata(
                origin_domain=Domain.CONTENT,
                target_domain=Domain.CORE,
                classification=DataClassification.INTERNAL,
                task_id=task_id,
                state=TaskState.DONE.value,
                duration_ms=4200,
            )
        )
        return SimpleNamespace(reel_id="ABCDE12345", state=TaskState.DONE)


class NullStore:
    def __init__(self, _path: Path) -> None:
        self.records: list[object] = []

    def append(self, record: object) -> None:
        self.records.append(record)


def test_end_to_end_a_reel_runs_through_the_content_worker_and_back(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = h.orchestrator.submit(ReelGoal(source_ref=REEL))
    pipeline = FinishingPipeline(h.mailbox(Domain.CONTENT))
    monkeypatch.setattr(content_cli, "_build", lambda a, d, t, m=None: pipeline)
    monkeypatch.setattr(content_cli, "JsonlReelStore", NullStore)

    code = content_cli.main(
        [
            "--data-dir",
            str(h.root / "content"),
            "serve",
            "--once",
            "--boundary-dir",
            str(h.root / "boundary"),
            "--task-public-key",
            public_key_hex(CORE_KEY),
        ]
    )
    assert code == 0 and pipeline.urls == [REEL]

    h.orchestrator.tick()
    task: TaskRecord | None = h.orchestrator.get(record.task_id)
    assert task is not None
    assert task.state is TaskState.DONE and task.duration_ms == 4200
    assert [t.state for t in h.telemetry] == [
        TelemetryState.PLANNED,
        TelemetryState.RUNNING,
        TelemetryState.DONE,
    ]


def test_the_status_command_lists_tasks_without_touching_mailboxes(
    h: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    from jarvis_core.orchestrator import __main__ as status_cli

    record = h.orchestrator.submit(ReelGoal(source_ref=REEL))
    h.report(record.task_id, "escalated", error="fetch_failed")
    h.orchestrator.sync()
    h.report(record.task_id, "done")  # stays queued: the command never reads mailboxes

    assert status_cli.main(["--data-dir", str(h.root / "core"), "status"]) == 0
    out = capsys.readouterr().out
    assert "escalated: 1" in out and "fetch_failed" in out and REEL not in out
    assert len(h.mailbox(Domain.CORE).receive(Domain.CONTENT)) == 1


def test_reported_cost_is_kept_as_a_running_total(h: Harness) -> None:
    record = h.orchestrator.submit(ReelGoal(source_ref=REEL))
    for cost in (0.01, 0.03, 0.02):  # a late, lower report never lowers the total
        h.mailbox(Domain.CONTENT).send(
            TaskMetadata(
                origin_domain=Domain.CONTENT,
                target_domain=Domain.CORE,
                classification=DataClassification.INTERNAL,
                task_id=record.task_id,
                state="running",
                duration_ms=10,
                cost_eur=cost,
            )
        )
    h.orchestrator.sync()
    task = h.orchestrator.get(record.task_id)
    assert task is not None and task.cost_eur == pytest.approx(0.03)
    assert h.telemetry[-1].cost_eur == pytest.approx(0.03)


# --- task tokens: core's task router is the only issuer ---------------------------------------


def test_dispatched_reels_carry_a_core_signed_token_for_exactly_that_task(h: Harness) -> None:
    orchestrator = Orchestrator(
        h.mailbox(Domain.CORE), None, clock=h.clock, tokens=TaskTokenIssuer(CORE_KEY)
    )
    record = orchestrator.submit(ReelGoal(source_ref=REEL))
    [letter] = h.mailbox(Domain.CONTENT).receive(Domain.CORE)
    event = letter.event
    assert isinstance(event, ReelSubmitted) and event.task_token is not None
    claims = VERIFIER.verify(event.task_token, now=h.clock.now)
    assert claims.task_id == record.task_id
    assert claims.domain is Domain.CONTENT
    assert claims.allowed_tools == REEL_TOOLS
    assert claims.taint is Taint.EXTERNAL_CONTENT and claims.budget_eur == 0.0
    # Valid for the task's deadline, so a reel can wait in the worker's queue.
    assert claims.expires_at == h.clock.now + DEFAULT_DEADLINE


def test_core_and_the_reel_worker_agree_on_the_reel_tools() -> None:
    assert ReelDispatcher.tools == REEL_TOOLS
