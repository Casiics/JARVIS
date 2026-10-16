"""Curation store contract and a file-backed implementation for a single workstation.

Each domain gives the pipeline its own store under its own data directory, so curated context
never mixes across domains. Production can back the same contract with the domain's Postgres.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from jarvis_core.curation.models import (
    CollectedItem,
    ContextEntry,
    DecisionBrief,
    DecisionRecord,
    Outcome,
)


class CurationStore(Protocol):
    def items(self) -> list[CollectedItem]: ...

    def add_items(self, items: list[CollectedItem]) -> None: ...

    def decisions(self) -> list[DecisionRecord]: ...

    def add_decision(self, record: DecisionRecord) -> None: ...

    def entries(self, category: str | None = None) -> list[ContextEntry]: ...

    def put_entry(self, entry: ContextEntry) -> None: ...


def consumed_item_ids(decisions: list[DecisionRecord]) -> set[str]:
    return {m for d in decisions for m in d.brief.digest.member_ids}


def open_reviews(decisions: list[DecisionRecord]) -> list[DecisionBrief]:
    """Briefs whose latest decision is still REVIEW, oldest first."""
    latest: dict[str, DecisionRecord] = {}
    for record in decisions:
        latest[record.brief.brief_id] = record
    return [r.brief for r in latest.values() if r.outcome is Outcome.REVIEW]


class FileCurationStore:
    """JSONL files: items.jsonl, decisions.jsonl and one context/<category>.jsonl per category."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._items = root / "items.jsonl"
        self._decisions = root / "decisions.jsonl"
        self._context = root / "context"

    def items(self) -> list[CollectedItem]:
        return _load(self._items, CollectedItem)

    def add_items(self, items: list[CollectedItem]) -> None:
        _append(self._items, items)

    def decisions(self) -> list[DecisionRecord]:
        return _load(self._decisions, DecisionRecord)

    def add_decision(self, record: DecisionRecord) -> None:
        _append(self._decisions, [record])

    def entries(self, category: str | None = None) -> list[ContextEntry]:
        if category is not None:
            return _load(self._category_file(category), ContextEntry)
        if not self._context.exists():
            return []
        out: list[ContextEntry] = []
        for file in sorted(self._context.glob("*.jsonl")):
            out.extend(_load(file, ContextEntry))
        return out

    def put_entry(self, entry: ContextEntry) -> None:
        """Insert or replace by entry_id. The category file is rewritten atomically."""
        file = self._category_file(entry.category)
        current = [e for e in _load(file, ContextEntry) if e.entry_id != entry.entry_id]
        current.append(entry)
        file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=file.parent, prefix=f".{file.name}.")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for item in current:
                fh.write(item.model_dump_json() + "\n")
        Path(tmp).replace(file)

    def _category_file(self, category: str) -> Path:
        if not category.replace("-", "").isalnum():
            raise ValueError("invalid category name")
        return self._context / f"{category}.jsonl"


def _load[M: BaseModel](path: Path, model: type[M]) -> list[M]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [model.model_validate_json(line) for line in fh if line.strip()]


def _append(path: Path, models: list[CollectedItem] | list[DecisionRecord]) -> None:
    if not models:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for item in models:
            fh.write(item.model_dump_json() + "\n")
