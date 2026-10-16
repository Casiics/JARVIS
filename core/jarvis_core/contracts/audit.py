"""Audit log entry. Holds references and hashes, never content."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.events import Taint


class AuditAction(StrEnum):
    TOOL_CALL = "tool_call"
    MODEL_CALL = "model_call"
    GATE_DECISION = "gate_decision"
    STATE_TRANSITION = "state_transition"
    APPROVAL = "approval"
    SELF_MODIFICATION = "self_modification"
    WORKFLOW_DEPLOY = "workflow_deploy"
    BOUNDARY_EVENT = "boundary_event"
    AUTH = "auth"


class AuditDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"


class AuditLogEntry(BaseModel):
    """One immutable audit record (EU AI Act Art. 12 traceability)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_id: UUID = Field(default_factory=uuid4)
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    trace_id: str = Field(max_length=64)
    task_id: UUID | None = None
    domain: Domain
    actor: str = Field(max_length=200, description="SPIFFE ID or user id")
    action: AuditAction
    target: str = Field(max_length=200)
    decision: AuditDecision
    policy_id: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=100)
    tokens: int | None = Field(default=None, ge=0)
    cost_eur: float | None = Field(default=None, ge=0)
    taint: Taint = Taint.NONE
    approval_ref: str | None = Field(default=None, max_length=100)
    content_hash: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", description="SHA-256 of the referenced content"
    )
    content_ref: str | None = Field(default=None, max_length=300)
