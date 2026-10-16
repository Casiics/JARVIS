"""Agent runtimes: the one seam between the supervisor-worker loop and whatever runs a model.

The loop (`jarvis_core.orchestrator.loop`) never talks to a model or an agent framework
directly. It hands an `AgentTurn` to an `AgentRuntime` and gets an `AgentReply` back, so the
same loop runs on:

- `GatewayRuntime`: one model call through the inference gateway (routing policy, FinOps
  circuit breaker, audit). Works today with the local Ollama models.
- `HermesRuntime` (`jarvis_core.agents.hermes`): a Hermes Agent instance per domain whose only
  model endpoint is a task-scoped gateway endpoint, so it is held to the same policy.
- a scripted runtime in tests, so the loop is testable without any model.

A runtime is bound to one domain at construction; a turn for another domain is refused.
Runtimes never get tools that act on the world: an intended effect comes back as data and
becomes an ActionRequest that the action gate decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import Taint
from jarvis_core.router.catalog import ModelClass
from jarvis_core.router.client import ChatMessage, ModelClientError
from jarvis_core.router.gateway import InferenceGateway
from jarvis_core.router.policy import RoutingDenied


class AgentRuntimeError(Exception):
    """The runtime could not produce a reply. The loop escalates with `error_class`."""

    def __init__(self, error_class: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class


class TurnScope(BaseModel):
    """Who is asking, for which task, under which label. Set by deterministic code only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID
    trace_id: str = Field(max_length=64)
    domain: Domain
    label: DataClassification
    taint: Taint = Taint.NONE


class AgentTurn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["worker", "supervisor"]
    model_class: ModelClass
    system: str
    prompt: str
    json_schema: dict[str, Any] | None = None
    max_output_tokens: int = Field(default=1024, gt=0, le=8192)


class AgentReply(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    model: str = Field(default="", max_length=100)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    cost_eur: float = Field(default=0.0, ge=0)


class AgentRuntime(Protocol):
    domain: Domain

    def run(self, turn: AgentTurn, scope: TurnScope) -> AgentReply: ...


def check_scope(runtime: AgentRuntime, scope: TurnScope) -> None:
    if scope.domain is not runtime.domain:
        raise AgentRuntimeError("runtime_domain_mismatch")


@dataclass
class GatewayRuntime:
    """One completion per turn through the inference gateway."""

    gateway: InferenceGateway
    domain: Domain
    require_local: bool = False
    prefer_cloud: bool = False

    def run(self, turn: AgentTurn, scope: TurnScope) -> AgentReply:
        check_scope(self, scope)
        model_class = turn.model_class
        if self.require_local and model_class is ModelClass.SUPERVISOR:
            # The routing policy sends supervisor calls to the cloud whenever the label allows
            # it. A runtime pinned to local models asks for the reasoning class instead, which
            # is the same local model the supervisor class uses.
            model_class = ModelClass.REASONING
        try:
            result = self.gateway.complete(
                caller_domain=self.domain,
                label=scope.label,
                model_class=model_class,
                messages=(
                    ChatMessage(role="system", content=turn.system),
                    ChatMessage(role="user", content=turn.prompt),
                ),
                task_id=scope.task_id,
                trace_id=scope.trace_id,
                taint=scope.taint,
                prefer_cloud=self.prefer_cloud,
                require_local=self.require_local or self.domain is Domain.PRIVATE,
                json_schema=turn.json_schema,
                max_output_tokens=turn.max_output_tokens,
            )
        except RoutingDenied as exc:
            raise AgentRuntimeError("routing_denied") from exc
        except ModelClientError as exc:
            raise AgentRuntimeError("model_unavailable") from exc
        return AgentReply(
            text=result.text,
            model=result.route.model[:100],
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cost_eur=result.cost_eur,
        )
