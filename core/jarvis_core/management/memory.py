"""The management layer's shared memory: short notes per domain and topic.

`MemoryPort` is the interface; the context pipeline's memory manager implements it on the
curation store. `JsonlMemory` is the simple default (one append-only file per domain) so the
management agents run without it. Notes hold metadata summaries only, never domain content,
and recall never returns a note above the caller's classification ceiling.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import Taint


class MemoryNote(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    domain: Domain
    topic: str = Field(pattern=r"^[a-z0-9:_-]{1,60}$")
    text: str = Field(min_length=1, max_length=600)
    label: DataClassification = DataClassification.INTERNAL
    taint: Taint = Taint.NONE
    evidence: tuple[UUID, ...] = Field(default=(), max_length=20)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MemoryPort(Protocol):
    def remember(self, note: MemoryNote) -> None: ...

    def recall(
        self,
        domain: Domain,
        topic_prefix: str,
        *,
        ceiling: DataClassification,
        limit: int = 5,
    ) -> list[MemoryNote]: ...


class JsonlMemory:
    """One JSON line per note, one file per domain. Newest notes are recalled first."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._lock = threading.Lock()

    def _file(self, domain: Domain) -> Path:
        return self._root / f"{domain.value}.jsonl"

    def remember(self, note: MemoryNote) -> None:
        path = self._file(note.domain)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(note.model_dump_json() + "\n")

    def recall(
        self,
        domain: Domain,
        topic_prefix: str,
        *,
        ceiling: DataClassification,
        limit: int = 5,
    ) -> list[MemoryNote]:
        path = self._file(domain)
        if not path.exists():
            return []
        notes: list[MemoryNote] = []
        for line in path.read_text("utf-8").splitlines():
            try:
                note = MemoryNote.model_validate_json(line)
            except ValidationError:
                continue  # a damaged line never breaks recall
            if note.topic.startswith(topic_prefix) and ceiling.at_least(note.label):
                notes.append(note)
        return notes[::-1][:limit]
