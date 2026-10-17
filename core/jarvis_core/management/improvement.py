"""The improvement agent: recurring failures -> lessons in shared memory -> skill proposals.

Self-learning works on the orchestrator's task records only, which are metadata (route, state,
error class, timings), never prompts or domain data. Deterministic code finds patterns: the
same route escalating with the same error class at least `min_count` times. For each pattern
with new evidence, a local model drafts one short lesson; Tier 0b checks it (right route, no
links, nothing that looks like a secret) and a local supervisor reviews it. The lesson is
stored as a `MemoryNote` with topic `lesson:<route>:<error_class>` and the escalated task ids
as evidence, so the delegation agent reads it back before its next decision.

Self-improvement goes one step further: once a route has lessons and enough verified (DONE)
runs, the agent proposes a skill for `skills/<domain>/` through the git gateway. It lands only
as a pull request a human merges; private-domain routes never become skills.
"""

from __future__ import annotations

import re
import threading
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from jarvis_core.agents.select import RuntimeChoice
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.task_token import TaskTokenClaims
from jarvis_core.management.delegation import (
    DESCRIPTIONS,
    AuditSink,
    management_claims,
    management_runtime,
)
from jarvis_core.management.memory import JsonlMemory, MemoryNote, MemoryPort
from jarvis_core.orchestrator.loop import (
    Acceptance,
    RuntimeSupervisor,
    RuntimeWorker,
    Supervisor,
    SupervisorWorkerLoop,
    Worker,
    WorkOrder,
)
from jarvis_core.orchestrator.service import TaskRecord
from jarvis_core.orchestrator.state_machine import TaskMachine, TaskState
from jarvis_core.selfimprove.skills import SkillProposal, SkillRejected, check, looks_like_secret

MAX_EVIDENCE = 20
SKILL_MIN_DONE = 3

# What an error class means, as deterministic context for the model.
HINTS: dict[str, str] = {
    "timeout": "the domain did not report back before the task's deadline",
    "max_iterations": "the worker's answers kept failing the acceptance checks",
    "model_unavailable": "the local model server was not reachable",
    "routing_denied": "the router refused the model call for this label",
    "job_not_available": "the domain has no handler for this job",
    "internal_error": "the domain's handler crashed",
    "fetch_failed": "the source could not be downloaded",
}

INSTRUCTION = (
    "A route keeps failing with the same error class. Write one short, practical lesson "
    "(one or two sentences) that helps the next decision on this route: when to send work "
    "there, what to check first, or what to tell the owner. Use only the facts given; no "
    "links, no secrets, no invented details. Set 'route' to the route key you were given."
)

_LINK = re.compile(r"https?://|www\.", re.IGNORECASE)


class LessonDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: str = Field(max_length=40)
    lesson: str = Field(min_length=20, max_length=400)


@dataclass(frozen=True)
class Pattern:
    route: str
    domain: Domain
    error_class: str
    task_ids: tuple[UUID, ...]

    @property
    def topic(self) -> str:
        return lesson_topic(self.route, self.error_class)


@dataclass
class ImprovementReport:
    lessons: list[str] = field(default_factory=list)  # topics written
    skills: list[str] = field(default_factory=list)  # skill paths proposed
    skipped: int = 0  # patterns without new evidence
    failed: list[str] = field(default_factory=list)  # topics the agents could not agree on


def lesson_topic(route: str, error_class: str) -> str:
    def slug(part: str) -> str:
        return re.sub(r"[^a-z0-9_-]+", "_", part.lower())

    return f"lesson:{slug(route)}:{slug(error_class)}"[:60]


def recurring(records: Sequence[TaskRecord], *, min_count: int = 2) -> list[Pattern]:
    """Escalations grouped by (route, error class), the most frequent first."""
    groups: dict[tuple[str, Domain, str], list[TaskRecord]] = defaultdict(list)
    for record in records:
        if record.state is TaskState.ESCALATED and record.error_class:
            groups[(record.kind, record.domain, record.error_class)].append(record)
    patterns = [
        Pattern(
            route=route,
            domain=domain,
            error_class=error_class,
            task_ids=tuple(
                r.task_id
                for r in sorted(group, key=lambda r: r.updated_at, reverse=True)[:MAX_EVIDENCE]
            ),
        )
        for (route, domain, error_class), group in groups.items()
        if len(group) >= min_count
    ]
    return sorted(patterns, key=lambda p: len(p.task_ids), reverse=True)


def acceptance_for(pattern: Pattern) -> Acceptance[LessonDraft]:
    def same_route(d: LessonDraft) -> str | None:
        return None if d.route == pattern.route else f"route must be '{pattern.route}'"

    def no_links(d: LessonDraft) -> str | None:
        return "lessons contain no links" if _LINK.search(d.lesson) else None

    def no_secrets(d: LessonDraft) -> str | None:
        return "the lesson looks like it contains a secret" if looks_like_secret(d.lesson) else None

    def one_paragraph(d: LessonDraft) -> str | None:
        return "one paragraph, no line breaks" if "\n" in d.lesson.strip() else None

    return Acceptance(LessonDraft, checks=(same_route, no_links, no_secrets, one_paragraph))


def skill_name(route: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", route.lower()).strip("-")[:30] + "-lessons"


@dataclass
class ImprovementAgent:
    tasks: Callable[[], Sequence[TaskRecord]]
    loop: SupervisorWorkerLoop
    worker: Worker
    supervisor: Supervisor | None
    memory: MemoryPort
    claims: Callable[[], TaskTokenClaims] = management_claims
    # Opens a skill pull request through the git gateway; None keeps it to lessons.
    propose: Callable[[SkillProposal, UUID], object] | None = None
    min_count: int = 2

    def review(self) -> ImprovementReport:
        report = ImprovementReport()
        records = list(self.tasks())
        touched: set[str] = set()
        for pattern in recurring(records, min_count=self.min_count):
            known = self._latest(pattern.topic)
            if known is not None and set(pattern.task_ids) <= set(known.evidence):
                report.skipped += 1
                continue
            draft = self._draft(pattern, known)
            if draft is None:
                report.failed.append(pattern.topic)
                continue
            self.memory.remember(
                MemoryNote(
                    domain=Domain.CORE,
                    topic=pattern.topic,
                    text=draft.lesson,
                    evidence=pattern.task_ids,
                )
            )
            report.lessons.append(pattern.topic)
            touched.add(pattern.route)
        if self.propose is not None:
            for route in sorted(touched):
                path = self._propose_skill(route, records)
                if path is not None:
                    report.skills.append(path)
        return report

    def _latest(self, topic: str) -> MemoryNote | None:
        notes = self.memory.recall(Domain.CORE, topic, ceiling=DataClassification.INTERNAL, limit=1)
        return notes[0] if notes else None

    def _draft(self, pattern: Pattern, known: MemoryNote | None) -> LessonDraft | None:
        claims = self.claims()
        facts = (
            f"route: {pattern.route}\n"
            f"what the route does: {DESCRIPTIONS.get(pattern.route, 'unknown route')}\n"
            f"error class: {pattern.error_class}\n"
            f"meaning: {HINTS.get(pattern.error_class, 'no description')}\n"
            f"escalations: {len(pattern.task_ids)}"
        )
        inputs: tuple[tuple[str, str], ...] = (("facts", facts),)
        if known is not None:
            inputs += (("previous_lesson", known.text),)
        order = WorkOrder(
            claims=claims,
            trace_id=claims.task_id.hex,
            instruction=INSTRUCTION,
            label=DataClassification.INTERNAL,
            inputs=inputs,
        )
        outcome = self.loop.run(order, acceptance_for(pattern), self.worker, self.supervisor)
        if outcome.state is not TaskState.DONE or outcome.value is None:
            return None
        return outcome.value

    def _propose_skill(self, route: str, records: Sequence[TaskRecord]) -> str | None:
        assert self.propose is not None
        done = [r for r in records if r.kind == route and r.state is TaskState.DONE]
        if len(done) < SKILL_MIN_DONE or done[0].domain is Domain.PRIVATE:
            return None
        lessons = self.memory.recall(
            Domain.CORE,
            lesson_topic(route, ""),
            ceiling=DataClassification.INTERNAL,
            limit=10,
        )
        if not lessons:
            return None
        name = skill_name(route)
        marker = f"skill:{name}"
        newest = max(n.created_at for n in lessons)
        proposed = self.memory.recall(
            Domain.CORE, marker, ceiling=DataClassification.INTERNAL, limit=1
        )
        if proposed and proposed[0].created_at >= newest:
            return None  # nothing learned since the last proposal
        body = (
            f"# Lessons for `{route}`\n\n"
            f"{DESCRIPTIONS.get(route, route)}.\n\n"
            "Learned from recurring escalations; read before sending work to this route.\n\n"
            + "\n".join(f"- {n.text}" for n in lessons)
        )
        evidence = tuple(r.task_id for r in done[:MAX_EVIDENCE])
        try:
            skill = SkillProposal(
                domain=done[0].domain,
                name=name,
                description=f"What the management layer learned about the {route} route",
                body=body,
                evidence=evidence,
            )
            check(skill)
        except (SkillRejected, ValueError):
            return None
        self.propose(skill, evidence[0])
        self.memory.remember(
            MemoryNote(
                domain=Domain.CORE,
                topic=marker,
                text=f"proposed {skill.path}",
                evidence=evidence,
            )
        )
        return skill.path


def build_improvement(
    tasks: Callable[[], Sequence[TaskRecord]],
    *,
    data_dir: Path,
    ollama_url: str,
    audit: AuditSink,
    runtime_choice: RuntimeChoice = "auto",
    propose: Callable[[SkillProposal, UUID], object] | None = None,
) -> ImprovementAgent:
    """The improvement agent as core runs it: same local runtime and memory as delegation."""
    runtime = management_runtime(
        data_dir=data_dir, ollama_url=ollama_url, audit=audit, choice=runtime_choice
    )
    return ImprovementAgent(
        tasks=tasks,
        loop=SupervisorWorkerLoop(
            TaskMachine(audit, actor="spiffe://jarvis.home/core/improvement")
        ),
        worker=RuntimeWorker(runtime),
        supervisor=RuntimeSupervisor(runtime),
        memory=JsonlMemory(data_dir / "memory"),
        propose=propose,
    )


def run_improvement_forever(
    agent: ImprovementAgent,
    interval_s: float,
    *,
    stop: threading.Event | None = None,
    on_report: Callable[[ImprovementReport], object] = lambda _r: None,
    on_error: Callable[[Exception], object] = lambda _e: None,
) -> None:
    """Review the task history every `interval_s`; one bad pass never stops the loop."""
    stop = stop or threading.Event()
    while not stop.wait(interval_s):
        try:
            on_report(agent.review())
        except Exception as exc:  # keep learning; the error is reported, not swallowed
            on_error(exc)
