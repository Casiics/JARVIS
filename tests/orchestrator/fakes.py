"""Fakes for the supervisor-worker loop: scripted runtimes and a recording model client."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from jarvis_core.agents.runtime import (
    AgentReply,
    AgentRuntimeError,
    AgentTurn,
    TurnScope,
    check_scope,
)
from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.events import Taint
from jarvis_core.contracts.task_token import TaskTokenClaims
from jarvis_core.orchestrator.loop import TOOL_SUPERVISOR, TOOL_WORKER
from jarvis_core.router.client import ModelCall, ModelResult


@dataclass
class ScriptedRuntime:
    """Answers worker and supervisor turns from two queues; records every turn."""

    domain: Domain
    worker: list[str] = field(default_factory=list)
    supervisor: list[str] = field(default_factory=list)
    cost_eur: float = 0.0
    fail_with: str | None = None
    turns: list[tuple[AgentTurn, TurnScope]] = field(default_factory=list)

    def run(self, turn: AgentTurn, scope: TurnScope) -> AgentReply:
        check_scope(self, scope)
        self.turns.append((turn, scope))
        if self.fail_with is not None:
            raise AgentRuntimeError(self.fail_with)
        queue = self.worker if turn.role == "worker" else self.supervisor
        text = queue.pop(0) if queue else "not json"
        return AgentReply(
            text=text,
            model="scripted",
            prompt_tokens=50,
            completion_tokens=10,
            cost_eur=self.cost_eur,
        )

    def roles(self) -> list[str]:
        return [t.role for t, _ in self.turns]


def claims(
    domain: Domain = Domain.CONTENT,
    *,
    budget_eur: float = 1.0,
    taint: Taint = Taint.NONE,
    tools: frozenset[str] = frozenset({TOOL_WORKER, TOOL_SUPERVISOR}),
    expires_in: timedelta = timedelta(minutes=15),
) -> TaskTokenClaims:
    return TaskTokenClaims(
        task_id=uuid4(),
        domain=domain,
        allowed_tools=tools,
        budget_eur=budget_eur,
        taint=taint,
        expires_at=datetime.now(UTC) + expires_in,
    )


@dataclass
class RecordingModel:
    """A model client that answers with fixed text and records each call."""

    answer: str = '{"ok": true}'
    calls: list[ModelCall] = field(default_factory=list)

    def complete(self, call: ModelCall) -> ModelResult:
        self.calls.append(call)
        return ModelResult(text=self.answer, prompt_tokens=30, completion_tokens=7)

    def loaded_models(self) -> list[str]:
        return []

    def unload(self, model: str) -> None:
        return None


class ExplodingCloud:
    def complete(self, call: ModelCall) -> ModelResult:
        raise AssertionError("cloud must not be called")

    def loaded_models(self) -> list[str]:
        return []

    def unload(self, model: str) -> None:
        return None


def trace() -> str:
    return uuid4().hex
