"""Signed task tokens: a worker may only do what its current task allows.

Two signatures exist:

- HMAC-SHA256 (`issue_token` / `verify_token`) for tokens issued and checked in one process,
  e.g. a backtest or a self-contained CLI run. Whoever can verify can also issue.
- Ed25519 (`issue_signed_token` / `Ed25519TokenVerifier`) for tokens that cross a domain
  boundary. Only core's task router holds the private key (`core/task_token_signing_key`, a 32-byte
  seed in hex); a domain worker gets the public key and can check tokens but never mint one.
  Signed tokens carry the prefix `ed25519.` so the two kinds cannot be confused.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.events import Taint

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

SIGNED_PREFIX = "ed25519."


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
    if token.startswith(SIGNED_PREFIX):
        raise TaskTokenError("expected an HMAC token, got a signed one")
    try:
        body, signature = token.split(".", 1)
    except ValueError as exc:
        raise TaskTokenError("malformed token") from exc
    if not hmac.compare_digest(_unb64(signature), _sign(key, body)):
        raise TaskTokenError("invalid signature")
    return _claims(body, now)


class TaskTokenVerifier(Protocol):
    def verify(self, token: str, *, now: datetime | None = None) -> TaskTokenClaims: ...


class HmacTokenVerifier:
    """Checks tokens issued in the same process with the same key."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    def verify(self, token: str, *, now: datetime | None = None) -> TaskTokenClaims:
        return verify_token(self._key, token, now=now)


def load_signing_key(seed_hex: str) -> Ed25519PrivateKey:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    try:
        seed = bytes.fromhex(seed_hex.strip())
    except ValueError as exc:
        raise TaskTokenError("task_token_signing_key must be hex") from exc
    if len(seed) != 32:
        raise TaskTokenError("task_token_signing_key must be 32 bytes of hex")
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_key_hex(key: Ed25519PrivateKey) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def load_verify_key(public_hex: str) -> Ed25519PublicKey:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        raw = bytes.fromhex(public_hex.strip())
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise TaskTokenError("task public key must be 32 bytes of hex") from exc


def issue_signed_token(
    key: Ed25519PrivateKey,
    *,
    task_id: UUID,
    domain: Domain,
    allowed_tools: frozenset[str],
    budget_eur: float,
    taint: Taint = Taint.NONE,
    ttl: timedelta = timedelta(minutes=15),
    now: datetime | None = None,
) -> str:
    """Issue an Ed25519 signed token. Only core's task router calls this."""
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
    return f"{SIGNED_PREFIX}{body}.{_b64(key.sign(body.encode()))}"


class Ed25519TokenVerifier:
    """Checks core-issued tokens with core's public key. Cannot issue any."""

    def __init__(self, public_key: Ed25519PublicKey) -> None:
        self._key = public_key

    def verify(self, token: str, *, now: datetime | None = None) -> TaskTokenClaims:
        from cryptography.exceptions import InvalidSignature

        if not token.startswith(SIGNED_PREFIX):
            raise TaskTokenError("token is not signed by core")
        try:
            body, signature = token[len(SIGNED_PREFIX) :].split(".", 1)
        except ValueError as exc:
            raise TaskTokenError("malformed token") from exc
        try:
            self._key.verify(_unb64(signature), body.encode())
        except InvalidSignature as exc:
            raise TaskTokenError("invalid signature") from exc
        return _claims(body, now)


def _claims(body: str, now: datetime | None) -> TaskTokenClaims:
    try:
        claims = TaskTokenClaims.model_validate_json(_unb64(body))
    except ValidationError as exc:
        raise TaskTokenError("malformed token") from exc
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
