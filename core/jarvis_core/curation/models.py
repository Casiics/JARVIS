"""Typed objects passed between the four curation stages."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import Taint

ID_PATTERN = r"^[A-Za-z0-9:_.-]{1,120}$"
SUBJECT_PATTERN = r"^[a-z0-9:_.-]{1,80}$"


class CollectedItem(BaseModel):
    """Stage 1 output: one piece of raw material from a domain source.

    The classification defaults to PRIVATE so a source that forgets the label fails closed.
    An item with a `subject` is a snapshot (for example computed market statistics): the newest
    snapshot of a subject replaces the stored entry instead of being merged into it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str = Field(pattern=ID_PATTERN)
    domain: Domain
    classification: DataClassification = DataClassification.PRIVATE
    taint: Taint = Taint.NONE
    source_ref: str = Field(min_length=1, max_length=500)
    collected_at: datetime
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=4000)
    facts: tuple[str, ...] = Field(default=(), max_length=40)
    tags: tuple[str, ...] = Field(default=(), max_length=40)
    signal: float = Field(ge=0.0, le=1.0, description="source-side usefulness estimate")
    flags: tuple[str, ...] = Field(default=(), description="e.g. prompt-injection markers")
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    subject: str | None = Field(default=None, pattern=SUBJECT_PATTERN)


class Digest(BaseModel):
    """Stage 2 output: a cluster of duplicate or near-duplicate items merged into one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    digest_id: str = Field(pattern=ID_PATTERN)
    domain: Domain
    classification: DataClassification
    taint: Taint
    member_ids: tuple[str, ...] = Field(min_length=1)
    duplicates_dropped: int = Field(ge=0)
    source_refs: tuple[str, ...] = Field(min_length=1)
    title: str
    summary: str
    facts: tuple[str, ...]
    tags: tuple[str, ...]
    signal: float = Field(ge=0.0, le=1.0)
    flags: tuple[str, ...]
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    subject: str | None = Field(default=None, pattern=SUBJECT_PATTERN)

    @property
    def corroboration(self) -> int:
        return len(self.source_refs)


class Proposal(StrEnum):
    STORE = "store"
    MERGE = "merge"
    DISCARD = "discard"


class Outcome(StrEnum):
    STORED = "stored"
    MERGED = "merged"
    DISCARDED = "discarded"
    REVIEW = "review"


class DecisionBrief(BaseModel):
    """Stage 3 output: everything the decision needs, computed deterministically."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    brief_id: str = Field(pattern=ID_PATTERN)
    digest: Digest
    category: str
    proposal: Proposal
    confidence: float = Field(ge=0.0, le=1.0)
    novelty: float = Field(ge=0.0, le=1.0)
    nearest_entry_id: str | None = None
    nearest_similarity: float = Field(default=0.0, ge=0.0, le=1.0)
    new_facts: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


class DecisionRecord(BaseModel):
    """Stage 4 output: what happened to a brief, by whom and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    brief: DecisionBrief
    outcome: Outcome
    decided_at: datetime
    decided_by: str = Field(max_length=200)
    entry_id: str | None = None
    reason: str = Field(max_length=300)


class ContextEntry(BaseModel):
    """One curated piece of context in a category. This is what agents later read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_id: str = Field(pattern=ID_PATTERN)
    domain: Domain
    category: str
    classification: DataClassification
    taint: Taint
    title: str
    summary: str
    facts: tuple[str, ...]
    tags: tuple[str, ...]
    source_refs: tuple[str, ...]
    digest_ids: tuple[str, ...]
    signal: float = Field(ge=0.0, le=1.0)
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    subject: str | None = Field(default=None, pattern=SUBJECT_PATTERN)


class CategoryRule(BaseModel):
    """A context category. Membership is decided by tags first, then keywords."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    description: str = Field(default="", max_length=300)
    tags: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    min_signal: float = Field(default=0.3, ge=0.0, le=1.0)
    auto_store_at: float = Field(default=0.7, ge=0.0, le=1.0)
    auto_discard_below: float = Field(default=0.35, ge=0.0, le=1.0)
    max_classification: DataClassification = DataClassification.INTERNAL
    max_age_days: int | None = Field(
        default=None,
        ge=1,
        description="entries not updated for this long stay stored but leave search and packs",
    )

    @model_validator(mode="after")
    def _ordered(self) -> CategoryRule:
        if self.auto_discard_below > self.auto_store_at:
            raise ValueError("auto_discard_below must not exceed auto_store_at")
        return self


class CurationConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    domain: Domain
    categories: tuple[CategoryRule, ...] = Field(min_length=1)
    default_category: str
    duplicate_similarity: float = Field(default=0.55, ge=0.0, le=1.0)
    merge_similarity: float = Field(default=0.45, ge=0.0, le=1.0)
    known_similarity: float = Field(default=0.85, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _default_exists(self) -> CurationConfig:
        names = [c.name for c in self.categories]
        if len(set(names)) != len(names):
            raise ValueError("category names must be unique")
        if self.default_category not in names:
            raise ValueError("default_category must be one of the categories")
        return self

    def category(self, name: str) -> CategoryRule:
        return next(c for c in self.categories if c.name == name)


class StageCounts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    collected: int = 0
    rejected: int = 0
    digests: int = 0
    duplicates_dropped: int = 0
    briefs: int = 0
    stored: int = 0
    merged: int = 0
    discarded: int = 0
    review: int = 0
