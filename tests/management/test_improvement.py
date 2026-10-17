"""The improvement agent: recurring escalations become lessons, lessons become skill PRs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import Job
from jarvis_core.management.improvement import ImprovementAgent, lesson_topic, recurring
from jarvis_core.management.memory import JsonlMemory
from jarvis_core.orchestrator.loop import RuntimeSupervisor, RuntimeWorker, SupervisorWorkerLoop
from jarvis_core.orchestrator.service import TaskRecord
from jarvis_core.orchestrator.state_machine import TaskMachine, TaskState
from jarvis_core.selfimprove.skills import SkillProposal
from tests.orchestrator.fakes import ScriptedRuntime, claims

OK = '{"accept": true}'
T0 = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
LESSON = "Reels from this source often fail to download; tell the owner and retry later."


def record(
    kind: str = "reel",
    state: TaskState = TaskState.ESCALATED,
    error: str | None = "fetch_failed",
    domain: Domain = Domain.CONTENT,
    minutes: int = 0,
) -> TaskRecord:
    at = T0 + timedelta(minutes=minutes)
    return TaskRecord(
        task_id=uuid4(),
        kind=kind,
        domain=domain,
        ref_hash="0" * 64,
        state=state,
        created_at=at,
        updated_at=at,
        deadline=at + timedelta(hours=1),
        error_class=error if state is TaskState.ESCALATED else None,
    )


def draft(route: str = "reel", lesson: str = LESSON) -> str:
    return f'{{"route": "{route}", "lesson": "{lesson}"}}'


class Rig:
    def __init__(self, root: Path, runtime: ScriptedRuntime, records: list[TaskRecord]) -> None:
        self.runtime = runtime
        self.records = records
        self.memory = JsonlMemory(root / "memory")
        self.proposed: list[tuple[SkillProposal, UUID]] = []
        self.agent = ImprovementAgent(
            tasks=lambda: self.records,
            loop=SupervisorWorkerLoop(TaskMachine(lambda _e: None, actor="test")),
            worker=RuntimeWorker(runtime),
            supervisor=RuntimeSupervisor(runtime),
            memory=self.memory,
            claims=lambda: claims(Domain.CORE, budget_eur=0.0),
            propose=lambda skill, task: self.proposed.append((skill, task)),
        )

    def lessons(self, route: str = "reel") -> list[str]:
        notes = self.memory.recall(
            Domain.CORE, lesson_topic(route, ""), ceiling=DataClassification.INTERNAL
        )
        return [n.text for n in notes]


def test_only_repeated_escalations_with_the_same_cause_are_patterns() -> None:
    records = [record(), record(minutes=1), record(error="timeout"), record(state=TaskState.DONE)]
    [pattern] = recurring(records)
    assert (pattern.route, pattern.error_class) == ("reel", "fetch_failed")
    assert pattern.task_ids == (records[1].task_id, records[0].task_id)
    assert pattern.topic == "lesson:reel:fetch_failed"


def test_a_recurring_failure_becomes_a_lesson_the_next_review_does_not_repeat(
    tmp_path: Path,
) -> None:
    runtime = ScriptedRuntime(Domain.CORE, worker=[draft()], supervisor=[OK])
    rig = Rig(tmp_path, runtime, [record(), record(minutes=1)])

    report = rig.agent.review()
    assert report.lessons == ["lesson:reel:fetch_failed"] and rig.lessons() == [LESSON]
    assert "fetch_failed" in runtime.turns[0][0].prompt  # facts, not domain data

    again = rig.agent.review()  # no new evidence: no model call
    assert (
        again.lessons == []
        and again.skipped == 1
        and runtime.roles()
        == [
            "worker",
            "supervisor",
        ]
    )


def test_new_evidence_refines_the_lesson_with_the_previous_one_as_input(tmp_path: Path) -> None:
    better = "Downloads from this source fail repeatedly; ask the owner for another link."
    runtime = ScriptedRuntime(
        Domain.CORE, worker=[draft(), draft(lesson=better)], supervisor=[OK, OK]
    )
    rig = Rig(tmp_path, runtime, [record(), record(minutes=1)])
    rig.agent.review()
    rig.records.append(record(minutes=2))

    assert rig.agent.review().lessons == ["lesson:reel:fetch_failed"]
    assert rig.lessons()[0] == better
    assert LESSON in runtime.turns[2][0].prompt


def test_tier_0b_refuses_links_and_the_wrong_route(tmp_path: Path) -> None:
    runtime = ScriptedRuntime(
        Domain.CORE,
        worker=[
            draft(lesson="Use https://example.com/mirror to fetch these reels instead."),
            draft(route="trading.paper_check"),
            draft(),
        ],
        supervisor=[OK],
    )
    rig = Rig(tmp_path, runtime, [record(), record(minutes=1)])

    assert rig.agent.review().lessons == ["lesson:reel:fetch_failed"]
    assert "no links" in runtime.turns[1][0].prompt
    assert "route must be 'reel'" in runtime.turns[2][0].prompt


def test_a_model_outage_writes_no_lesson(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        ScriptedRuntime(Domain.CORE, fail_with="model_unavailable"),
        [record(), record(minutes=1)],
    )
    report = rig.agent.review()
    assert report.failed == ["lesson:reel:fetch_failed"] and rig.lessons() == []


def test_lessons_with_verified_runs_are_proposed_as_a_skill_once(tmp_path: Path) -> None:
    done = [record(state=TaskState.DONE, minutes=m) for m in range(3)]
    runtime = ScriptedRuntime(Domain.CORE, worker=[draft()], supervisor=[OK])
    rig = Rig(tmp_path, runtime, [record(), record(minutes=1), *done])

    report = rig.agent.review()
    [(skill, task)] = rig.proposed
    assert report.skills == ["skills/content/reel-lessons/SKILL.md"] == [skill.path]
    assert LESSON in skill.body and set(skill.evidence) == {r.task_id for r in done}
    assert task in skill.evidence

    rig.agent.review()
    assert len(rig.proposed) == 1  # nothing learned since the proposal


def test_private_routes_learn_but_never_become_skills(tmp_path: Path) -> None:
    route = Job.PRIVATE_DAILY_BRIEFING.value
    records = [
        record(kind=route, domain=Domain.PRIVATE, error="timeout", minutes=m) for m in range(2)
    ] + [record(kind=route, domain=Domain.PRIVATE, state=TaskState.DONE) for _ in range(3)]
    lesson = "The private domain often misses its deadline; check that its job server runs."
    runtime = ScriptedRuntime(Domain.CORE, worker=[draft(route, lesson)], supervisor=[OK])
    rig = Rig(tmp_path, runtime, records)

    report = rig.agent.review()
    assert report.lessons == ["lesson:private_daily_briefing:timeout"]
    assert report.skills == [] and rig.proposed == []
