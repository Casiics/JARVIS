"""Passkey identity for the command deck: credentials, sessions and one-time ceremonies.

JARVIS has one human owner. Every login and every approval is a WebAuthn ceremony with user
verification required. Session tokens and ceremony ids are random, stored only as hashes, and
ceremonies are single use.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from jarvis_core.approvals.grant import Decision

OWNER = "owner"


def sha256_hex(data: bytes | str) -> str:
    raw = data.encode() if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


class StoredCredential(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", ser_json_bytes="base64", val_json_bytes="base64"
    )

    credential_id: bytes
    public_key: bytes
    sign_count: int
    user_id: str
    created_at: datetime
    label: str = ""

    @property
    def credential_hash(self) -> str:
        return sha256_hex(self.credential_id)


class CredentialStore(Protocol):
    def add(self, credential: StoredCredential) -> None: ...

    def get(self, credential_id: bytes) -> StoredCredential | None: ...

    def for_user(self, user_id: str) -> list[StoredCredential]: ...

    def update_sign_count(self, credential_id: bytes, sign_count: int) -> None: ...


class InMemoryCredentialStore:
    def __init__(self) -> None:
        self._items: dict[bytes, StoredCredential] = {}

    def add(self, credential: StoredCredential) -> None:
        if credential.credential_id in self._items:
            raise ValueError("credential already registered")
        self._items[credential.credential_id] = credential

    def get(self, credential_id: bytes) -> StoredCredential | None:
        return self._items.get(credential_id)

    def for_user(self, user_id: str) -> list[StoredCredential]:
        return [c for c in self._items.values() if c.user_id == user_id]

    def update_sign_count(self, credential_id: bytes, sign_count: int) -> None:
        current = self._items[credential_id]
        self._items[credential_id] = current.model_copy(update={"sign_count": sign_count})


@dataclass(frozen=True)
class Session:
    token_hash: str
    user_id: str
    credential_hash: str
    created_at: datetime
    expires_at: datetime


class SessionStore:
    """Server-side sessions. Only the hash of the cookie value is kept."""

    def __init__(self) -> None:
        self._items: dict[str, Session] = {}

    def create(
        self, user_id: str, credential_hash: str, now: datetime, ttl: timedelta
    ) -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        session = Session(sha256_hex(token), user_id, credential_hash, now, now + ttl)
        self._items[session.token_hash] = session
        return token, session

    def get(self, token: str | None, now: datetime) -> Session | None:
        if not token:
            return None
        session = self._items.get(sha256_hex(token))
        if session is None or session.expires_at <= now:
            return None
        return session

    def revoke(self, token: str | None) -> None:
        if token:
            self._items.pop(sha256_hex(token), None)


class CeremonyKind(StrEnum):
    REGISTER = "register"
    LOGIN = "login"
    APPROVAL = "approval"


@dataclass(frozen=True)
class Ceremony:
    kind: CeremonyKind
    challenge: bytes
    expires_at: datetime
    session_hash: str | None = None
    approval_id: UUID | None = None
    decision: Decision | None = None
    request_hash: str | None = None
    bootstrap: bool = False


class CeremonyStore:
    """One-time WebAuthn challenges. take() removes the ceremony whatever happens next."""

    def __init__(self) -> None:
        self._items: dict[str, Ceremony] = {}

    def put(self, ceremony: Ceremony) -> str:
        ceremony_id = secrets.token_urlsafe(24)
        self._items[sha256_hex(ceremony_id)] = ceremony
        return ceremony_id

    def take(self, ceremony_id: str, kind: CeremonyKind, now: datetime) -> Ceremony | None:
        ceremony = self._items.pop(sha256_hex(ceremony_id), None)
        if ceremony is None or ceremony.kind is not kind or ceremony.expires_at <= now:
            return None
        return ceremony


def approval_challenge(approval_id: UUID, decision: Decision, request_hash: str) -> bytes:
    """Random challenge that also commits to what is being decided."""
    nonce = secrets.token_bytes(32)
    binding = f"jarvis-approval\0{approval_id}\0{decision.value}\0{request_hash}".encode()
    return hashlib.sha256(nonce + binding).digest()
