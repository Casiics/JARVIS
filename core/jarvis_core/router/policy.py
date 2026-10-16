"""Routing policy. Protected path: the privacy boundary between local and cloud models.

Rules:
- SECRET content is never sent to any model.
- The private domain and PRIVATE content use local models only. There is no cloud route.
- CONFIDENTIAL content stays local unless the deterministic gate pseudonymised it.
- Cloud-bound prompts are scanned; any PII or secret pattern blocks the call.
- Unknown labels are treated as PRIVATE (fail closed).
"""

from __future__ import annotations

from dataclasses import dataclass

from jarvis_core.contracts.classification import DataClassification, Domain, classify
from jarvis_core.router.catalog import (
    LOCAL_MODELS,
    LONG_CONTEXT_CODING,
    Endpoint,
    ModelClass,
)
from jarvis_core.router.pii import find_pii


class RoutingDenied(Exception):
    """The call must not happen at all."""


@dataclass(frozen=True)
class RouteRequest:
    caller_domain: Domain  # from the workload identity, never from the payload
    label: str | DataClassification | None
    model_class: ModelClass
    prompt: str
    prefer_cloud: bool = False
    pseudonymised: bool = False
    context_tokens: int = 0


@dataclass(frozen=True)
class Route:
    endpoint: Endpoint
    model: str
    classification: DataClassification
    reason: str


def route(request: RouteRequest, cloud_models: dict[ModelClass, str]) -> Route:
    cls = classify(request.caller_domain, request.label)

    if cls is DataClassification.SECRET:
        raise RoutingDenied("SECRET content is never sent to a model")

    cloud_allowed, why_local = _cloud_allowed(request, cls)
    wants_cloud = request.prefer_cloud or request.model_class is ModelClass.SUPERVISOR
    if wants_cloud and cloud_allowed:
        model = cloud_models.get(request.model_class)
        if model is None:
            raise RoutingDenied(f"no cloud model configured for {request.model_class}")
        hits = find_pii(request.prompt)
        if hits:
            raise RoutingDenied(f"cloud call blocked by PII scanner: {', '.join(hits)}")
        return Route(Endpoint.CLOUD, model, cls, "cloud allowed for this classification")

    local = LOCAL_MODELS[request.model_class]
    if request.model_class is ModelClass.CODING and request.context_tokens > local.max_context:
        local = LONG_CONTEXT_CODING
    if request.context_tokens > local.max_context:
        raise RoutingDenied("prompt exceeds local context and cloud is not allowed")
    return Route(Endpoint.LOCAL, local.name, cls, why_local or "local by preference")


def _cloud_allowed(request: RouteRequest, cls: DataClassification) -> tuple[bool, str]:
    if request.caller_domain is Domain.PRIVATE:
        return False, "private domain has no cloud route"
    if cls.at_least(DataClassification.PRIVATE):
        return False, "PRIVATE content stays local"
    if cls is DataClassification.CONFIDENTIAL and not request.pseudonymised:
        return False, "CONFIDENTIAL content stays local unless pseudonymised"
    return True, ""
