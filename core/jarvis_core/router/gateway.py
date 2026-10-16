"""Inference gateway: the single path from any domain to a model.

Combines the routing policy, the FinOps circuit breaker, the model clients and the audit
writer, so a caller cannot reach a model without all four. Every call, allowed or denied,
produces one audit entry holding hashes and counts only.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from jarvis_core.contracts.audit import AuditAction, AuditDecision, AuditLogEntry
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import Taint
from jarvis_core.router.budget import BudgetExceeded, CircuitBreaker
from jarvis_core.router.catalog import Endpoint, ModelClass
from jarvis_core.router.client import ChatMessage, ModelCall, ModelClient
from jarvis_core.router.policy import Route, RouteRequest, RoutingDenied, route

AuditSink = Callable[[AuditLogEntry], object]


@dataclass(frozen=True)
class CloudPricing:
    """EUR per 1,000 tokens for one cloud model id."""

    input_per_1k: float
    output_per_1k: float


@dataclass(frozen=True)
class GatewayResult:
    text: str
    route: Route
    prompt_tokens: int
    completion_tokens: int
    cost_eur: float
    duration_ms: int


@dataclass
class InferenceGateway:
    local: ModelClient
    breaker: CircuitBreaker
    audit: AuditSink
    actor: str
    cloud: ModelClient | None = None
    cloud_models: dict[ModelClass, str] = field(default_factory=dict)
    cloud_pricing: dict[str, CloudPricing] = field(default_factory=dict)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def complete(
        self,
        *,
        caller_domain: Domain,
        label: DataClassification | str | None,
        model_class: ModelClass,
        messages: tuple[ChatMessage, ...],
        task_id: UUID,
        trace_id: str,
        taint: Taint = Taint.NONE,
        prefer_cloud: bool = False,
        require_local: bool = False,
        json_schema: dict[str, Any] | None = None,
        max_output_tokens: int = 1024,
    ) -> GatewayResult:
        prompt_text = "\n".join(m.content for m in messages)
        digest = _prompt_digest(messages)
        request = RouteRequest(
            caller_domain=caller_domain,
            label=label,
            model_class=model_class,
            prompt=prompt_text,
            prefer_cloud=prefer_cloud and not require_local,
            context_tokens=_estimate_tokens(messages),
        )
        target = f"{caller_domain.value}:{model_class.value}"

        def deny(reason: str) -> RoutingDenied:
            self._write(
                caller_domain,
                task_id,
                trace_id,
                target,
                AuditDecision.DENY,
                taint,
                digest,
                policy_id=reason[:100],
            )
            return RoutingDenied(reason)

        try:
            chosen = route(request, self.cloud_models)
        except RoutingDenied as exc:
            raise deny(str(exc)) from exc
        if require_local and chosen.endpoint is not Endpoint.LOCAL:
            raise deny("caller requires a local model")

        now = self.clock()
        client = self.local if chosen.endpoint is Endpoint.LOCAL else self.cloud
        if client is None:
            raise deny("no cloud client configured")
        pricing = self.cloud_pricing.get(chosen.model)
        if chosen.endpoint is Endpoint.CLOUD:
            if pricing is None:
                raise deny(f"no pricing configured for {chosen.model}")
            estimate = (
                request.context_tokens * pricing.input_per_1k
                + max_output_tokens * pricing.output_per_1k
            ) / 1000
            try:
                self.breaker.check(caller_domain, estimate, now)
            except BudgetExceeded as exc:
                raise deny(str(exc)) from exc

        call = ModelCall(
            model=chosen.model,
            messages=messages,
            json_schema=json_schema,
            max_output_tokens=max_output_tokens,
        )
        result = client.complete(call)
        duration_ms = int((self.clock() - now).total_seconds() * 1000)

        cost = 0.0
        if pricing is not None and chosen.endpoint is Endpoint.CLOUD:
            cost = (
                result.prompt_tokens * pricing.input_per_1k
                + result.completion_tokens * pricing.output_per_1k
            ) / 1000
            self.breaker.record(caller_domain, cost, self.clock())

        self._write(
            caller_domain,
            task_id,
            trace_id,
            target,
            AuditDecision.ALLOW,
            taint,
            digest,
            model=chosen.model,
            tokens=result.prompt_tokens + result.completion_tokens,
            cost_eur=round(cost, 6),
        )
        return GatewayResult(
            text=result.text,
            route=chosen,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cost_eur=cost,
            duration_ms=max(duration_ms, 0),
        )

    def unload_local_models(self) -> list[str]:
        """Free the GPU, e.g. before a Faster-Whisper batch job. Returns what was unloaded."""
        unloaded = self.local.loaded_models()
        for model in unloaded:
            self.local.unload(model)
        return unloaded

    def _write(
        self,
        domain: Domain,
        task_id: UUID,
        trace_id: str,
        target: str,
        decision: AuditDecision,
        taint: Taint,
        digest: str,
        *,
        policy_id: str | None = None,
        model: str | None = None,
        tokens: int | None = None,
        cost_eur: float | None = None,
    ) -> None:
        self.audit(
            AuditLogEntry(
                trace_id=trace_id,
                task_id=task_id,
                domain=domain,
                actor=self.actor,
                action=AuditAction.MODEL_CALL,
                target=target,
                decision=decision,
                policy_id=policy_id,
                model=model,
                tokens=tokens,
                cost_eur=cost_eur,
                taint=taint,
                content_hash=digest,
            )
        )


def _prompt_digest(messages: tuple[ChatMessage, ...]) -> str:
    h = hashlib.sha256()
    for m in messages:
        h.update(m.role.encode())
        h.update(b"\0")
        h.update(m.content.encode("utf-8"))
        for image in m.images_b64:
            h.update(b"\0img\0")
            h.update(image.encode())
    return h.hexdigest()


def _estimate_tokens(messages: tuple[ChatMessage, ...]) -> int:
    # Roughly 4 characters per token; each image costs about 1,000 tokens in qwen2.5-vl.
    chars = sum(len(m.content) for m in messages)
    images = sum(len(m.images_b64) for m in messages)
    return chars // 4 + images * 1000
