"""Memory steward: the management role that keeps a domain's shared memory.

Every agent of a domain shares that domain's curated context. The steward is the one place they
reach it through:

- `recall` attaches fitting, fresh context to a work order before a run;
- `remember` turns lessons from an accepted run into collected items and sends them through the
  four curation stages, so agents propose memory and deterministic code decides (CLAUDE.md 2.5);
- `maintain` reports the memory's health (size, stale entries, open reviews, audit chain) for a
  periodic management task.

Memory is per domain on purpose: there is no store shared across domains (CLAUDE.md 2.1). A
lesson never gets a lower label than the work order it came from, and inherits its taint.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from jarvis_core.audit.chain import AuditChainError, verify_chain
from jarvis_core.audit.file import FileAuditStore
from jarvis_core.contracts.classification import DataClassification
from jarvis_core.curation.context_input import select_context, with_context
from jarvis_core.curation.models import CollectedItem, StageCounts
from jarvis_core.curation.pipeline import CurationPipeline
from jarvis_core.curation.retrieval import fresh
from jarvis_core.curation.similarity import fingerprint
from jarvis_core.curation.store import open_reviews
from jarvis_core.orchestrator.loop import WorkOrder
from jarvis_core.quarantine import injection_flags

MAX_LESSONS = 10


class StewardError(ValueError):
    """The request does not fit this steward's domain."""


class Lesson(BaseModel):
    """Something an agent learned in a run that later runs should know."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=4000)
    facts: tuple[str, ...] = Field(default=(), max_length=20)
    tags: tuple[str, ...] = Field(default=(), max_length=20)
    signal: float = Field(default=0.6, ge=0.0, le=1.0)
    classification: DataClassification = DataClassification.PRIVATE


class MemoryHealth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: dict[str, int]
    stale: dict[str, int]
    open_reviews: int
    audit_chain_intact: bool | None


@dataclass
class MemorySteward:
    pipeline: CurationPipeline
    audit_file: FileAuditStore | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    @property
    def domain(self) -> str:
        return self.pipeline.config.domain.value

    def recall(
        self,
        order: WorkOrder,
        query: str | None = None,
        *,
        category: str | None = None,
        k: int = 8,
        max_chars: int = 6000,
    ) -> WorkOrder:
        """The order with the domain's fitting, fresh context as its first input."""
        self._check(order)
        entries = fresh(self.pipeline.store.entries(category), self.pipeline.config, self.clock())
        context = select_context(
            entries,
            domain=order.claims.domain,
            ceiling=order.label,
            query=query,
            category=category,
            k=k,
            max_chars=max_chars,
        )
        return with_context(order, context)

    def remember(self, order: WorkOrder, lessons: Sequence[Lesson]) -> StageCounts:
        """Run the lessons of an accepted run through the four curation stages."""
        self._check(order)
        if len(lessons) > MAX_LESSONS:
            raise StewardError(f"at most {MAX_LESSONS} lessons per run")
        now = self.clock()
        items = []
        for n, lesson in enumerate(lessons):
            label = max(lesson.classification, order.label, key=lambda c: c.rank)
            text = "\n".join((lesson.title, lesson.summary, *lesson.facts))
            items.append(
                CollectedItem(
                    item_id=f"lesson:{order.claims.task_id.hex}:{n}",
                    domain=order.claims.domain,
                    classification=label,
                    taint=order.claims.taint,
                    source_ref=f"task:{order.claims.task_id}",
                    collected_at=now,
                    title=lesson.title,
                    summary=lesson.summary,
                    facts=lesson.facts,
                    tags=lesson.tags,
                    signal=lesson.signal,
                    # Agent output may carry injected instructions: flagged lessons go to review.
                    flags=injection_flags(text),
                    content_hash=fingerprint(lesson.summary, *lesson.facts),
                )
            )
        return self.pipeline.run(items)

    def maintain(self) -> MemoryHealth:
        store, config = self.pipeline.store, self.pipeline.config
        entries = store.entries()
        current = {e.entry_id for e in fresh(entries, config, self.clock())}
        counts: dict[str, int] = {}
        stale: dict[str, int] = {}
        for entry in entries:
            counts[entry.category] = counts.get(entry.category, 0) + 1
            if entry.entry_id not in current:
                stale[entry.category] = stale.get(entry.category, 0) + 1
        intact: bool | None = None
        if self.audit_file is not None:
            try:
                intact = verify_chain(self.audit_file.entries()) is None
            except AuditChainError:
                intact = False
        return MemoryHealth(
            entries=counts,
            stale=stale,
            open_reviews=len(open_reviews(store.decisions())),
            audit_chain_intact=intact,
        )

    def _check(self, order: WorkOrder) -> None:
        if order.claims.domain is not self.pipeline.config.domain:
            raise StewardError(f"this steward keeps the {self.domain} domain's memory")


__all__ = ["Lesson", "MemoryHealth", "MemorySteward", "StewardError"]
