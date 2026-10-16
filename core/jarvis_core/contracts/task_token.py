"""Signed task tokens: a worker may only do what its current task allows."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.events import Taint


class TaskTokenError(Exception):
    """Raised for malformed, forged or expired tokens."""


class TaskTokenClaims(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID
    domain: Domain
    allowed_tools: frozenset[str]
    budget_eur: float = Field(ge=0)
    taint: Taint = Taint.NONE
    expires_at: datetime


def issue_token(
    key: bytes,
    *,
    task_id: UUID,
    domain: Domain,
    allowed_tools: frozenset[str],
    budget_eur: float,
    taint: Taint = Taint.NONE,
    ttl: timedelta = timedelta(minutes=15),
    now: datetime | None = None,
) -> str:
    """Issue an HMAC-SHA256 signed token. The key comes from the core domain's secrets."""
    if len(key) < 32:
        raise TaskTokenError("signing key must be at least 32 bytes")
    issued = now or datetime.now(UTC)
    claims = TaskTokenClaims(
        task_id=task_id,
        domain=domain,
        allowed_tools=allowed_tools,
        budget_eur=budget_eur,
        taint=taint,
        expires_at=issued + ttl,
    )
    body = _b64(_canonical(claims))
    return f"{body}.{_b64(_sign(key, body))}"


def verify_token(key: bytes, token: str, *, now: datetime | None = None) -> TaskTokenClaims:
    try:
        body, signature = token.split(".", 1)
    except ValueError as exc:
        raise TaskTokenError("malformed token") from exc
    if not hmac.compare_digest(_unb64(signature), _sign(key, body)):
        raise TaskTokenError("invalid signature")
    claims = TaskTokenClaims.model_validate_json(_unb64(body))
    if claims.expires_at <= (now or datetime.now(UTC)):
        raise TaskTokenError("token expired")
    return claims


def _canonical(claims: TaskTokenClaims) -> bytes:
    data = claims.model_dump(mode="json")
    data["allowed_tools"] = sorted(data["allowed_tools"])
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def _sign(key: bytes, body: str) -> bytes:
    return hmac.new(key, body.encode(), hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError as exc:
        raise TaskTokenError("malformed token") from exc
