"""Core's record of every domain's latest audit anchor (AuditAnchor events).

Each anchor is also written to core's central audit log by the orchestrator; this book only
remembers the latest one per (domain, chain) so a chain that goes backwards is caught the
moment its next anchor arrives, not only on a later manual check.
"""

from __future__ import annotations

import threading
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.events import AuditAnchor
from jarvis_core.storage import write_atomic


class AnchorVerdict(StrEnum):
    FIRST = "first"
    UNCHANGED = "unchanged"
    ADVANCED = "advanced"
    # Fewer entries than last time, or the same count with a different head: the local
    # chain was truncated or rewritten since the previous anchor.
    ROLLED_BACK = "rolled_back"


class Anchor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    domain: Domain
    chain: str = Field(max_length=40)
    entries: int = Field(ge=0)
    head_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


_ANCHORS = TypeAdapter(list[Anchor])


class AnchorBook:
    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._latest: dict[tuple[Domain, str], Anchor] = {}
        if path is not None and path.exists():
            for anchor in _ANCHORS.validate_json(path.read_bytes()):
                self._latest[(anchor.domain, anchor.chain)] = anchor

    def record(self, origin: Domain, event: AuditAnchor) -> AnchorVerdict:
        """Judge the new anchor against the last one, then keep it if the chain moved forward.

        A rolled-back anchor is not stored: the last good head stays the reference, so the
        chain keeps failing until a human looks at it.
        """
        new = Anchor(
            domain=origin, chain=event.chain, entries=event.entries, head_hash=event.head_hash
        )
        with self._lock:
            old = self._latest.get((origin, event.chain))
            if old is None:
                verdict = AnchorVerdict.FIRST
            elif new.entries < old.entries or (
                new.entries == old.entries and new.head_hash != old.head_hash
            ):
                return AnchorVerdict.ROLLED_BACK
            elif new.entries == old.entries:
                return AnchorVerdict.UNCHANGED
            else:
                verdict = AnchorVerdict.ADVANCED
            self._latest[(origin, event.chain)] = new
            if self._path is not None:
                write_atomic(self._path, _ANCHORS.dump_json(list(self._latest.values())))
            return verdict

    def latest(self) -> list[Anchor]:
        with self._lock:
            return list(self._latest.values())
