"""The delegation agent: plain words in, one specialised agent out, decided by code."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from jarvis_core.boundary.mailbox import Mailbox
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import Job, ReelSubmitted, TaskAssigned
from jarvis_core.management import delegation
from jarvis_core.management.delegation import DelegationAgent, DelegationStatus
from jarvis_core.management.memory import JsonlMemory, MemoryNote
from jarvis_core.orchestrator.jobs import JobOutcome, JobServer
from jarvis_core.orchestrator.loop import RuntimeSupervisor, RuntimeWorker, SupervisorWorkerLoop
from jarvis_core.orchestrator.service import Orchestrator
from jarvis_core.orchestrator.state_machine import TaskMachine, TaskState
from tests.orchestrator.fakes import ScriptedRuntime, claims

REEL = "https://www.instagram.com/reel/ABCDE12345/"
OK = '{"accept": true}'


class Rig:
    def __init__(self, root: Path, runtime: ScriptedRuntime) -> None:
        self.root = root
        self.runtime = runtime
        self.orchestrator = Orchestrator(
            Mailbox(root / "boundary", Domain.CORE),
            None,
            clock=lambda: datetime(2026, 10, 17, 9, 0, tzinfo=UTC),
        )
        self.memory = JsonlMemory(root / "memory")
        self.agent = DelegationAgent(
            orchestrator=self.orchestrator,
            loop=SupervisorWorkerLoop(TaskMachine(lambda _e: None, actor="test")),
            worker=RuntimeWorker(runtime),
            supervisor=RuntimeSupervisor(runtime),
            claims=lambda: claims(Domain.CORE, budget_eur=0.0),
            memory=self.memory,
        )

    def sent(self, domain: Domain) -> list[object]:
        box = Mailbox(self.root / "boundary", domain)
        return [letter.event for letter in box.receive(Domain.CORE)]


def plan(route: str, url: str | None = None) -> str:
    link = f'"{url}"' if url else "null"
    return f'{{"route": "{route}", "reel_url": {link}, "reason": "fits the request"}}'


def test_a_bare_reel_link_is_dispatched_without_a_model(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ScriptedRuntime(Domain.CORE))
    result = rig.agent.delegate(f"  {REEL}?igsh=x  ", actor="user:owner")

    assert result.status is DelegationStatus.DISPATCHED and not result.used_model
    assert rig.runtime.turns == []
    [event] = rig.sent(Domain.CONTENT)
    assert isinstance(event, ReelSubmitted) and event.source_ref == REEL


def test_the_model_picks_the_route_and_code_dispatches_it(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ScriptedRuntime(Domain.CORE, worker=[plan("reel", REEL)], supervisor=[OK]))
    result = rig.agent.delegate(f"Fass mir bitte dieses Reel zusammen: {REEL}", actor="u")

    assert result.status is DelegationStatus.DISPATCHED and result.domain is Domain.CONTENT
    assert rig.runtime.roles() == ["worker", "supervisor"]
    assert "content domain" in rig.runtime.turns[0][0].prompt  # the catalogue was offered
    [note] = rig.memory.recall(Domain.CORE, "delegation", ceiling=DataClassification.INTERNAL)
    assert note.evidence == (result.task_id,)


def test_a_link_the_request_never_contained_is_refused_by_tier_0b(tmp_path: Path) -> None:
    invented = "https://www.instagram.com/reel/ZZZZZ99999/"
    rig = Rig(
        tmp_path,
        ScriptedRuntime(
            Domain.CORE, worker=[plan("reel", invented), plan("reel", REEL)], supervisor=[OK]
        ),
    )
    result = rig.agent.delegate(f"Bitte das hier verarbeiten: {REEL}", actor="u")

    assert result.status is DelegationStatus.DISPATCHED
    assert "must appear in the request" in rig.runtime.turns[1][0].prompt


def test_jobs_wait_for_the_flow_rule_and_nothing_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(delegation, "route_allowed", lambda _key: False)
    route = Job.PRIVATE_DAILY_BRIEFING.value
    rig = Rig(tmp_path, ScriptedRuntime(Domain.CORE, worker=[plan(route)], supervisor=[OK]))
    result = rig.agent.delegate("Mach mir mein Briefing für heute", actor="u")

    assert result.status is DelegationStatus.NEEDS_APPROVAL and result.domain is Domain.PRIVATE
    assert result.task_id is None and rig.orchestrator.tasks() == []


def test_requests_no_specialist_fits_stay_unclear(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ScriptedRuntime(Domain.CORE, worker=[plan("none")], supervisor=[OK]))
    result = rig.agent.delegate("Wie wird das Wetter?", actor="u")
    assert result.status is DelegationStatus.UNCLEAR and rig.orchestrator.tasks() == []


def test_a_model_outage_is_reported_not_guessed(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ScriptedRuntime(Domain.CORE, fail_with="model_unavailable"))
    result = rig.agent.delegate("Mach was mit Trading", actor="u")
    assert result.status is DelegationStatus.UNCLEAR
    assert result.error_class == "model_unavailable"


def test_lessons_from_memory_reach_the_worker(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ScriptedRuntime(Domain.CORE, worker=[plan("none")], supervisor=[OK]))
    rig.memory.remember(
        MemoryNote(domain=Domain.CORE, topic="lesson:reel:fetch_failed", text="Reels from x fail")
    )
    rig.agent.delegate("irgendwas", actor="u")
    assert "Reels from x fail" in rig.runtime.turns[0][0].prompt


def test_memory_recall_respects_the_ceiling(tmp_path: Path) -> None:
    memory = JsonlMemory(tmp_path)
    memory.remember(
        MemoryNote(
            domain=Domain.CORE, topic="a", text="secret-ish", label=DataClassification.CONFIDENTIAL
        )
    )
    assert memory.recall(Domain.CORE, "a", ceiling=DataClassification.INTERNAL) == []
    assert len(memory.recall(Domain.CORE, "a", ceiling=DataClassification.CONFIDENTIAL)) == 1


def _assignment(job: Job) -> TaskAssigned:
    return TaskAssigned(
        origin_domain=Domain.CORE,
        target_domain=Domain.TRADING,
        classification=DataClassification.INTERNAL,
        task_id=uuid4(),
        job=job,
    )


def test_a_domain_job_server_reports_running_then_the_outcome(tmp_path: Path) -> None:
    mailbox = Mailbox(tmp_path, Domain.TRADING)
    server = JobServer(mailbox, {Job.TRADING_PAPER_CHECK: lambda _e: JobOutcome()}, log=print)
    outcome = server.run(_assignment(Job.TRADING_PAPER_CHECK))

    assert outcome.state is TaskState.DONE
    reports = [lt.event for lt in Mailbox(tmp_path, Domain.CORE).receive(Domain.TRADING)]
    assert [getattr(r, "state", None) for r in reports] == ["running", "done"]


def test_a_job_server_refuses_other_domains_jobs() -> None:
    with pytest.raises(ValueError, match="another domain"):
        JobServer(
            Mailbox(Path("."), Domain.TRADING),
            {Job.PRIVATE_DAILY_BRIEFING: lambda _e: JobOutcome()},
        )
