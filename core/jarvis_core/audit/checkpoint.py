"""Signed checkpoints of the audit chain (hourly) and their verification.

A checkpoint signs the chain's head at a sequence number with Ed25519:

    "jarvis-audit-checkpoint/v1\n<upto_seq>\n<head_hash>\n<signed_at ISO>\n<key_id>"

Because every entry hash covers all earlier entries, one signature vouches for the whole log up
to `upto_seq`. Someone who can write to the database but does not hold the signing key can
still append entries, but cannot rewrite history without breaking an existing signature. The
key is core's `audit_signing_key` (32-byte Ed25519 seed, hex); the public key is kept off the
server as well, so verification does not depend on the server being honest.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from jarvis_core.audit.chain import GENESIS_HASH, ChainedEntry, verify_chain

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

# cryptography is imported inside the functions that sign or verify, so domain services that
# only store checkpoints (FileCheckpointStore) do not need it (extra "audit").
DOMAIN_TAG = "jarvis-audit-checkpoint/v1"


class CheckpointError(Exception):
    pass


class Checkpoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    upto_seq: int = Field(ge=0, description="number of entries covered (0 for an empty log)")
    head_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    signed_at: AwareDatetime
    key_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    signature: str = Field(min_length=1, max_length=200, description="base64 Ed25519")


class CheckpointStore(Protocol):
    def last(self) -> Checkpoint | None: ...

    def append(self, checkpoint: Checkpoint) -> None: ...

    def all(self) -> list[Checkpoint]: ...


def load_private_key(seed_hex: str) -> Ed25519PrivateKey:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    seed = bytes.fromhex(seed_hex.strip())
    if len(seed) != 32:
        raise CheckpointError("audit_signing_key must be 32 bytes of hex")
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_key_hex(key: Ed25519PublicKey) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return key.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def load_public_key(hex_text: str) -> Ed25519PublicKey:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    raw = bytes.fromhex(hex_text.strip())
    if len(raw) != 32:
        raise CheckpointError("an Ed25519 public key is 32 bytes of hex")
    return Ed25519PublicKey.from_public_bytes(raw)


def key_id(key: Ed25519PublicKey) -> str:
    return hashlib.sha256(bytes.fromhex(public_key_hex(key))).hexdigest()[:16]


def message(upto_seq: int, head_hash: str, signed_at: datetime, kid: str) -> bytes:
    stamp = signed_at.astimezone(UTC).isoformat()
    return f"{DOMAIN_TAG}\n{upto_seq}\n{head_hash}\n{stamp}\n{kid}".encode()


def sign(key: Ed25519PrivateKey, upto_seq: int, head_hash: str, now: datetime) -> Checkpoint:
    if now.tzinfo is None:
        raise CheckpointError("signed_at must be timezone-aware")
    kid = key_id(key.public_key())
    signature = key.sign(message(upto_seq, head_hash, now, kid))
    return Checkpoint(
        upto_seq=upto_seq,
        head_hash=head_hash,
        signed_at=now,
        key_id=kid,
        signature=base64.b64encode(signature).decode(),
    )


def check_signature(checkpoint: Checkpoint, keys: Mapping[str, Ed25519PublicKey]) -> bool:
    from cryptography.exceptions import InvalidSignature

    key = keys.get(checkpoint.key_id)
    if key is None:
        return False
    try:
        key.verify(
            base64.b64decode(checkpoint.signature, validate=True),
            message(
                checkpoint.upto_seq, checkpoint.head_hash, checkpoint.signed_at, checkpoint.key_id
            ),
        )
    except (InvalidSignature, ValueError):
        return False
    return True


def next_checkpoint(
    entries_count: int,
    head_hash: str,
    store: CheckpointStore,
    key: Ed25519PrivateKey,
    now: datetime,
) -> Checkpoint:
    """Sign the current head. Runs hourly even when nothing changed, as a liveness signal."""
    previous = store.last()
    if previous is not None:
        if entries_count < previous.upto_seq:
            raise CheckpointError("the log is shorter than the last checkpoint")
        if entries_count == previous.upto_seq and head_hash != previous.head_hash:
            raise CheckpointError("the head changed without new entries")
        if now <= previous.signed_at:
            raise CheckpointError("checkpoint time must increase")
    checkpoint = sign(key, entries_count, head_hash, now)
    store.append(checkpoint)
    return checkpoint


@dataclass(frozen=True)
class Finding:
    """One problem found by `verify_log`; `index` is a checkpoint or entry position."""

    kind: str
    index: int
    detail: str


def verify_log(
    entries: Sequence[ChainedEntry],
    checkpoints: Sequence[Checkpoint],
    keys: Mapping[str, Ed25519PublicKey],
) -> list[Finding]:
    """Check the chain links, every checkpoint's signature, and that each signed head is the
    entry hash at its position. An empty list means the log is intact and vouched for."""
    findings: list[Finding] = []
    broken = verify_chain(list(entries))
    if broken is not None:
        findings.append(Finding("chain", broken, "entry does not link to its predecessor"))
    last_seq, last_time = -1, None
    for index, checkpoint in enumerate(checkpoints):
        if not check_signature(checkpoint, keys):
            findings.append(Finding("signature", index, "unknown key or invalid signature"))
        if checkpoint.upto_seq < last_seq or (
            last_time is not None and checkpoint.signed_at <= last_time
        ):
            findings.append(Finding("order", index, "checkpoints go backwards"))
        last_seq, last_time = checkpoint.upto_seq, checkpoint.signed_at
        if checkpoint.upto_seq > len(entries):
            findings.append(Finding("missing", index, "log ends before this checkpoint"))
            continue
        expected = (
            entries[checkpoint.upto_seq - 1].entry_hash if checkpoint.upto_seq else GENESIS_HASH
        )
        if expected != checkpoint.head_hash:
            findings.append(Finding("head", index, "signed head differs from the log"))
    return findings


class InMemoryCheckpointStore:
    def __init__(self) -> None:
        self.items: list[Checkpoint] = []

    def last(self) -> Checkpoint | None:
        return self.items[-1] if self.items else None

    def append(self, checkpoint: Checkpoint) -> None:
        self.items.append(checkpoint)

    def all(self) -> list[Checkpoint]:
        return list(self.items)
