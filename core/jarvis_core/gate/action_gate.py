"""Deterministic decision on ActionRequests. No LLM is involved here."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from jarvis_core.contracts.actions import ActionRequest, ApprovalTier
from jarvis_core.contracts.events import Taint
from jarvis_core.contracts.task_token import TaskTokenClaims


class GateOutcome(StrEnum):
    EXECUTE = "execute"
    NEEDS_APPROVAL = "needs_approval"
    NEEDS_STEP_UP = "needs_step_up"
    REJECT = "reject"


@dataclass(frozen=True)
class GateDecision:
    outcome: GateOutcome
    reason: str


# Tools no agent may ever request, whatever its token says.
FORBIDDEN_TOOLS: frozenset[str] = frozenset(
    {"withdraw_funds", "manage_api_keys", "read_credentials", "push_main", "edit_policy"}
)


def decide(request: ActionRequest, claims: TaskTokenClaims) -> GateDecision:
    if request.tool in FORBIDDEN_TOOLS:
        return GateDecision(GateOutcome.REJECT, f"{request.tool} is forbidden")
    if request.domain is not claims.domain or request.task_id != claims.task_id:
        return GateDecision(GateOutcome.REJECT, "request does not match task token")
    if request.tool not in claims.allowed_tools:
        return GateDecision(GateOutcome.REJECT, f"{request.tool} not in task token")

    tainted = request.taint is not Taint.NONE or claims.taint is not Taint.NONE
    if request.tier is ApprovalTier.D:
        return GateDecision(GateOutcome.NEEDS_STEP_UP, "tier D needs human step-up")
    if request.tier is ApprovalTier.C:
        return GateDecision(GateOutcome.NEEDS_APPROVAL, "tier C needs human approval")
    if tainted:
        return GateDecision(
            GateOutcome.NEEDS_APPROVAL, "task saw external content; human approval required"
        )
    return GateDecision(GateOutcome.EXECUTE, f"tier {request.tier} runs automatically")
