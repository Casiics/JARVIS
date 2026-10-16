"""Pending approvals. The action gate's held requests land here and wait for the deck."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from jarvis_core.approvals.grant import Decision
from jarvis_core.contracts.actions import ActionRequest
from jarvis_core.gate.action_gate import GateDecision, GateOutcome


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PendingApproval(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    approval_id: UUID = Field(default_factory=uuid4)
    request: ActionRequest
    gate_outcome: GateOutcome
    gate_reason: str = Field(max_length=300)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_at: datetime | None = None
    decided_by: str | None = None
    grant_token: str | None = None


class ApprovalConflict(Exception):
    """The approval does not exist or was already decided."""


class ApprovalStore(Protocol):
    def add(self, approval: PendingApproval) -> None: ...

    def get(self, approval_id: UUID) -> PendingApproval | None: ...

    def pending(self) -> list[PendingApproval]: ...

    def decide(
        self, approval_id: UUID, decision: Decision, by: str, grant_token: str, at: datetime
    ) -> PendingApproval:
        """Atomically move a pending approval to its decision. Raises ApprovalConflict."""
        ...


class InMemoryApprovalStore:
    """Development store. Production keeps approvals in pg-core with the same contract."""

    def __init__(self) -> None:
        self._items: dict[UUID, PendingApproval] = {}

    def add(self, approval: PendingApproval) -> None:
        if approval.status is not ApprovalStatus.PENDING:
            raise ApprovalConflict("only pending approvals can be added")
        self._items[approval.approval_id] = approval

    def get(self, approval_id: UUID) -> PendingApproval | None:
        return self._items.get(approval_id)

    def pending(self) -> list[PendingApproval]:
        items = [a for a in self._items.values() if a.status is ApprovalStatus.PENDING]
        return sorted(items, key=lambda a: a.created_at)

    def decide(
        self, approval_id: UUID, decision: Decision, by: str, grant_token: str, at: datetime
    ) -> PendingApproval:
        current = self._items.get(approval_id)
        if current is None or current.status is not ApprovalStatus.PENDING:
            raise ApprovalConflict("approval is not pending")
        status = (
            ApprovalStatus.APPROVED if decision is Decision.APPROVE else ApprovalStatus.REJECTED
        )
        decided = current.model_copy(
            update={
                "status": status,
                "decided_at": at,
                "decided_by": by,
                "grant_token": grant_token,
            }
        )
        self._items[approval_id] = decided
        return decided


def hold(store: ApprovalStore, request: ActionRequest, decision: GateDecision) -> PendingApproval:
    """Queue a request the action gate held. Requests the gate executes or rejects never wait."""
    if decision.outcome not in (GateOutcome.NEEDS_APPROVAL, GateOutcome.NEEDS_STEP_UP):
        raise ValueError(f"gate outcome {decision.outcome.value} does not need approval")
    approval = PendingApproval(
        request=request, gate_outcome=decision.outcome, gate_reason=decision.reason[:300]
    )
    store.add(approval)
    return approval
