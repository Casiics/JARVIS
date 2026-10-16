"""Append-only JSONL audit chain inside a domain's own volume.

Domain services cannot reach pg-audit (net-core) yet, so their entries (metadata only:
hashes, counts, states) are chained locally until the audit writer runs as a service.
Used by the private assistant and the trading executor.
"""

from __future__ import annotations

import json
from pathlib import Path

from jarvis_core.audit.chain import (
    GENESIS_HASH,
    AuditChainError,
    ChainedEntry,
    compute_hash,
    verify_chain,
)


class FileAuditStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        entries = self.entries()
        broken = verify_chain(entries)
        if broken is not None:
            raise AuditChainError(f"audit file is broken at entry {broken}")
        self._head = entries[-1].entry_hash if entries else GENESIS_HASH

    def head(self) -> str:
        return self._head

    def append(self, chained: ChainedEntry) -> None:
        if chained.prev_hash != self._head:
            raise AuditChainError("prev_hash does not match current head")
        if chained.entry_hash != compute_hash(chained.prev_hash, chained.payload):
            raise AuditChainError("entry_hash does not match payload")
        line = json.dumps(
            {"payload": chained.payload, "prev": chained.prev_hash, "hash": chained.entry_hash}
        )
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self._path.chmod(0o600)
        self._head = chained.entry_hash

    def entries(self) -> list[ChainedEntry]:
        if not self._path.exists():
            return []
        out: list[ChainedEntry] = []
        with self._path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    item = json.loads(line)
                    out.append(ChainedEntry(item["payload"], item["prev"], item["hash"]))
        return out
