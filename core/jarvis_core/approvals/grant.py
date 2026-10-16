"""Approval grants: the only proof an executor accepts that a human approved an action.

A grant is issued by the API gateway after a passkey ceremony bound to one approval and one
decision. It is HMAC-signed with the gateway's grant key, names the exact ActionRequest by
hash, expires quickly and can be used once. The execution guard refuses tier C and D actions,
and tainted actions the gate held, without a valid grant.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jarvis_core.contracts.actions import ActionRequest
from jarvis_core.gate.action_gate import GateDecision, GateOutcome

GRANT_TTL = timedelta(minutes=10)


class GrantError(Exception):
    """The grant is missing, forged, expired, reused or for another request."""


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ApprovalGrant(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    grant_id: UUID = Field(default_factory=uuid4)
    approval_id: UUID
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Decision
    approver: str = Field(max_length=200, description="user id of the passkey owner")
    credential_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: datetime
    expires_at: datetime


def request_hash(request: ActionRequest) -> str:
    """SHA-256 over the canonical request, so a grant covers exactly these parameters."""
    data = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def sign_grant(key: bytes, grant: ApprovalGrant) -> str:
    if len(key) < 32:
        raise GrantError("grant key must be at least 32 bytes")
    body = _b64(grant.model_dump_json().encode())
    return f"{body}.{_b64(_mac(key, body))}"


def read_grant(key: bytes, token: str) -> ApprovalGrant:
    try:
        body, signature = token.split(".", 1)
        valid = hmac.compare_digest(_unb64(signature), _mac(key, body))
    except (ValueError, TypeError) as exc:
        raise GrantError("malformed grant") from exc
    if not valid:
        raise GrantError("invalid grant signature")
    try:
        return ApprovalGrant.model_validate_json(_unb64(body))
    except (ValidationError, ValueError) as exc:
        raise GrantError("malformed grant") from exc


class UsedGrants(Protocol):
    def use(self, grant_id: UUID) -> bool:
        """Mark the grant used. False if it was used before."""
        ...


class InMemoryUsedGrants:
    def __init__(self) -> None:
        self._seen: set[UUID] = set()

    def use(self, grant_id: UUID) -> bool:
        if grant_id in self._seen:
            return False
        self._seen.add(grant_id)
        return True


def requires_grant(decision: GateDecision) -> bool:
    return decision.outcome in (GateOutcome.NEEDS_APPROVAL, GateOutcome.NEEDS_STEP_UP)


def authorize_execution(
    request: ActionRequest,
    decision: GateDecision,
    grant_token: str | None,
    *,
    key: bytes,
    used: UsedGrants,
    now: datetime | None = None,
) -> ApprovalGrant | None:
    """Raise GrantError unless the request may run now. Returns the grant that allowed it."""
    if decision.outcome is GateOutcome.REJECT:
        raise GrantError(f"gate rejected the request: {decision.reason}")
    if decision.outcome is GateOutcome.EXECUTE:
        return None
    if grant_token is None:
        raise GrantError(f"{decision.outcome.value}: a human approval from the deck is required")
    grant = read_grant(key, grant_token)
    current = now or datetime.now(UTC)
    if grant.expires_at <= current:
        raise GrantError("grant expired")
    if grant.request_hash != request_hash(request):
        raise GrantError("grant is for a different request")
    if grant.decision is not Decision.APPROVE:
        raise GrantError("the request was rejected by a human")
    if not used.use(grant.grant_id):
        raise GrantError("grant was already used")
    return grant


def _mac(key: bytes, body: str) -> bytes:
    return hmac.new(key, b"jarvis-approval-grant\0" + body.encode(), hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
