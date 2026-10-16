"""Hash chain over audit entries: entry_hash = SHA-256(prev_hash || canonical payload)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from jarvis_core.contracts.audit import AuditLogEntry

GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class ChainedEntry:
    payload: str
    prev_hash: str
    entry_hash: str


def canonical_payload(entry: AuditLogEntry) -> str:
    """Deterministic JSON text. This exact text is hashed and stored."""
    return json.dumps(entry.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def compute_hash(prev_hash: str, payload: str) -> str:
    return hashlib.sha256((prev_hash + payload).encode("utf-8")).hexdigest()


class AuditStore(Protocol):
    def head(self) -> str:
        """Hash of the newest entry, or GENESIS_HASH for an empty log."""
        ...

    def append(self, chained: ChainedEntry) -> None:
        """Persist the entry. Must reject it if prev_hash is not the current head."""
        ...


class AuditChainError(Exception):
    pass


class InMemoryAuditStore:
    """Test and development store with the same contract as the Postgres store."""

    def __init__(self) -> None:
        self.entries: list[ChainedEntry] = []

    def head(self) -> str:
        return self.entries[-1].entry_hash if self.entries else GENESIS_HASH

    def append(self, chained: ChainedEntry) -> None:
        if chained.prev_hash != self.head():
            raise AuditChainError("prev_hash does not match current head")
        if chained.entry_hash != compute_hash(chained.prev_hash, chained.payload):
            raise AuditChainError("entry_hash does not match payload")
        self.entries.append(chained)


class AuditWriter:
    """The only writer of the audit log."""

    def __init__(self, store: AuditStore) -> None:
        self._store = store

    def write(self, entry: AuditLogEntry) -> ChainedEntry:
        payload = canonical_payload(entry)
        prev = self._store.head()
        chained = ChainedEntry(payload, prev, compute_hash(prev, payload))
        self._store.append(chained)
        return chained


def verify_chain(entries: list[ChainedEntry]) -> int | None:
    """Return the index of the first broken entry, or None if the chain is intact."""
    prev = GENESIS_HASH
    for index, item in enumerate(entries):
        if item.prev_hash != prev or item.entry_hash != compute_hash(prev, item.payload):
            return index
        prev = item.entry_hash
    return None
