"""The delegation agent: an owner's request in plain words -> the right specialised agent.

A local model reads the request and the catalogue of routes the orchestrator knows (a reel for
the content domain, the predefined jobs of trading and private) and proposes one route. Tier 0b
checks the proposal deterministically: the route must exist, and a reel link must appear
verbatim in the request (a model never invents one). A local supervisor does the semantic QA.
Deterministic code then builds the typed goal and hands it to the orchestrator; a route the
flow policy does not allow yet comes back as `needs_approval` instead of being sent.

A request that is nothing but a reel link skips the model entirely.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from jarvis_core.agents.runtime import AgentRuntime
from jarvis_core.agents.select import RuntimeChoice, local_runtime
from jarvis_core.contracts.audit import AuditLogEntry
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import Job
from jarvis_core.contracts.reel_url import InvalidReelUrl, canonical_reel_url
from jarvis_core.contracts.task_token import TaskTokenClaims, issue_token, verify_token
from jarvis_core.management.memory import JsonlMemory, MemoryNote, MemoryPort
from jarvis_core.orchestrator.loop import (
    TOOL_SUPERVISOR,
    TOOL_WORKER,
    Acceptance,
    RuntimeSupervisor,
    RuntimeWorker,
    Supervisor,
    SupervisorWorkerLoop,
    Worker,
    WorkOrder,
)
from jarvis_core.orchestrator.service import (
    ROUTES,
    Goal,
    JobGoal,
    Orchestrator,
    ReelGoal,
    route_allowed,
)
from jarvis_core.orchestrator.state_machine import TaskMachine, TaskState
from jarvis_core.router.budget import CircuitBreaker, FileSpendLedger
from jarvis_core.router.client import OllamaClient
from jarvis_core.router.gateway import InferenceGateway

AuditSink = Callable[[AuditLogEntry], object]
NO_ROUTE = "none"
MAX_REQUEST = 2000
_URL = re.compile(r"https?://\S+")

# What each route's specialist does, as the model sees it. New routes need a line here.
DESCRIPTIONS: dict[str, str] = {
    "reel": "content domain: download, transcribe and summarise one Instagram reel link",
    Job.PRIVATE_DAILY_BRIEFING.value: (
        "private domain: sync calendar and mail, sort new mail, write today's briefing"
    ),
    Job.TRADING_PAPER_CHECK.value: (
        "trading domain: check the paper account, open positions and the kill switch"
    ),
}

INSTRUCTION = (
    "Pick the one specialised agent that should handle the owner's request. Answer with the "
    f"route key from the catalogue, or '{NO_ROUTE}' if none fits. For the 'reel' route, copy "
    "the reel link exactly as it appears in the request. Give a one-sentence reason."
)


class DelegationStatus(StrEnum):
    DISPATCHED = "dispatched"
    NEEDS_APPROVAL = "needs_approval"  # the flow policy does not allow this route yet
    ALREADY_RUNNING = "already_running"
    UNCLEAR = "unclear"  # no route fits, or the agents could not agree on one


class DelegationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: str = Field(max_length=40)
    reel_url: str | None = Field(default=None, max_length=300)
    reason: str = Field(min_length=3, max_length=300)


@dataclass(frozen=True)
class DelegationResult:
    status: DelegationStatus
    route: str | None
    domain: Domain | None
    task_id: UUID | None
    reason: str
    error_class: str | None = None
    used_model: bool = True


def reel_links(text: str) -> list[str]:
    """Canonical reel links that appear verbatim in the text."""
    found: list[str] = []
    for raw in _URL.findall(text):
        try:
            canonical = canonical_reel_url(raw.rstrip(".,;)"))
        except InvalidReelUrl:
            continue
        if canonical not in found:
            found.append(canonical)
    return found


def acceptance_for(request: str) -> Acceptance[DelegationPlan]:
    links = reel_links(request)

    def known_route(p: DelegationPlan) -> str | None:
        if p.route == NO_ROUTE or p.route in DESCRIPTIONS:
            return None
        return f"route must be one of {sorted(DESCRIPTIONS)} or '{NO_ROUTE}'"

    def reel_link_from_request(p: DelegationPlan) -> str | None:
        if p.route != "reel":
            return "only the reel route takes a reel_url" if p.reel_url else None
        if p.reel_url is None:
            return "the reel route needs the reel link from the request"
        try:
            canonical = canonical_reel_url(p.reel_url)
        except InvalidReelUrl:
            return "reel_url is not an Instagram reel link"
        return None if canonical in links else "reel_url must appear in the request"

    return Acceptance(DelegationPlan, checks=(known_route, reel_link_from_request), max_chars=2000)


def catalogue() -> str:
    return "\n".join(f"- {key}: {DESCRIPTIONS[key]}" for key in sorted(DESCRIPTIONS))


@dataclass
class DelegationAgent:
    orchestrator: Orchestrator
    loop: SupervisorWorkerLoop
    worker: Worker
    supervisor: Supervisor | None
    claims: Callable[[], TaskTokenClaims]
    memory: MemoryPort | None = None
    # Reels go through the deck's reel desk when there is one, so they show up under Content
    # with their link and can be retried; it returns the task id.
    reels: Callable[[str], UUID] | None = None

    def delegate(self, request: str, *, actor: str) -> DelegationResult:
        request = request.strip()[:MAX_REQUEST]
        links = reel_links(request)
        if links and _URL.sub("", request).strip() == "" and len(links) == 1:
            plan = DelegationPlan(route="reel", reel_url=links[0], reason="the request is a link")
            return self._dispatch(plan, actor, used_model=False)
        claims = self.claims()
        order = WorkOrder(
            claims=claims,
            trace_id=claims.task_id.hex,
            instruction=INSTRUCTION,
            label=DataClassification.INTERNAL,
            inputs=(("request", request), ("catalogue", catalogue()), *self._lessons()),
        )
        outcome = self.loop.run(order, acceptance_for(request), self.worker, self.supervisor)
        if outcome.state is not TaskState.DONE or outcome.value is None:
            return DelegationResult(
                DelegationStatus.UNCLEAR, None, None, None, "", outcome.error_class or "unclear"
            )
        return self._dispatch(outcome.value, actor, used_model=True)

    def _lessons(self) -> tuple[tuple[str, str], ...]:
        if self.memory is None:
            return ()
        notes = self.memory.recall(
            Domain.CORE, "lesson:", ceiling=DataClassification.INTERNAL, limit=5
        )
        if not notes:
            return ()
        return (("lessons", "\n".join(f"- {n.text}" for n in notes)),)

    def _dispatch(self, plan: DelegationPlan, actor: str, *, used_model: bool) -> DelegationResult:
        if plan.route == NO_ROUTE:
            return DelegationResult(
                DelegationStatus.UNCLEAR, None, None, None, plan.reason, used_model=used_model
            )
        goal: Goal = (
            ReelGoal(source_ref=plan.reel_url or "")
            if plan.route == "reel"
            else JobGoal(job=Job(plan.route))
        )
        domain = ROUTES[plan.route].domain
        if not route_allowed(plan.route):
            result = DelegationResult(
                DelegationStatus.NEEDS_APPROVAL,
                plan.route,
                domain,
                None,
                plan.reason,
                used_model=used_model,
            )
        elif (running := self.orchestrator.open_task(plan.route)) is not None and isinstance(
            goal, JobGoal
        ):
            result = DelegationResult(
                DelegationStatus.ALREADY_RUNNING,
                plan.route,
                domain,
                running.task_id,
                plan.reason,
                used_model=used_model,
            )
        else:
            if isinstance(goal, ReelGoal) and self.reels is not None:
                task_id = self.reels(goal.source_ref)
            else:
                task_id = self.orchestrator.submit(goal, actor=actor).task_id
            result = DelegationResult(
                DelegationStatus.DISPATCHED,
                plan.route,
                domain,
                task_id,
                plan.reason,
                used_model=used_model,
            )
        self._remember(result)
        return result

    def _remember(self, result: DelegationResult) -> None:
        if self.memory is None or result.route is None:
            return
        self.memory.remember(
            MemoryNote(
                domain=Domain.CORE,
                topic="delegation",
                text=f"{result.route} -> {result.status.value}: {result.reason}"[:600],
                evidence=(result.task_id,) if result.task_id else (),
            )
        )


def build_delegation(
    orchestrator: Orchestrator,
    *,
    data_dir: Path,
    ollama_url: str,
    audit: AuditSink,
    runtime_choice: RuntimeChoice = "auto",
    reels: Callable[[str], UUID] | None = None,
) -> DelegationAgent:
    """The delegation agent as core runs it: local models only, Hermes when installed."""
    runtime = management_runtime(
        data_dir=data_dir, ollama_url=ollama_url, audit=audit, choice=runtime_choice
    )
    return DelegationAgent(
        orchestrator=orchestrator,
        loop=SupervisorWorkerLoop(TaskMachine(audit, actor="spiffe://jarvis.home/core/delegation")),
        worker=RuntimeWorker(runtime),
        supervisor=RuntimeSupervisor(runtime),
        claims=management_claims,
        memory=JsonlMemory(data_dir / "memory"),
        reels=reels,
    )


def management_runtime(
    *, data_dir: Path, ollama_url: str, audit: AuditSink, choice: RuntimeChoice = "auto"
) -> AgentRuntime:
    """The runtime every management agent shares: local models, one spend ledger."""
    gateway = InferenceGateway(
        local=OllamaClient(ollama_url),
        breaker=CircuitBreaker(FileSpendLedger(data_dir / "management-spend.jsonl")),
        audit=audit,
        actor="spiffe://jarvis.home/core/management",
    )
    return local_runtime(gateway, Domain.CORE, choice=choice)


def management_claims() -> TaskTokenClaims:
    """A short-lived, local token for one management decision: the loop's two steps only."""
    key = secrets.token_bytes(32)
    return verify_token(
        key,
        issue_token(
            key,
            task_id=uuid4(),
            domain=Domain.CORE,
            allowed_tools=frozenset({TOOL_WORKER, TOOL_SUPERVISOR}),
            budget_eur=0.0,
            ttl=timedelta(minutes=10),
        ),
    )
