"""Boundary events: the only way data crosses from one domain to another."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.reel_url import canonical_reel_url


class Taint(StrEnum):
    """Provenance marker. External content is treated as untrusted input."""

    NONE = "none"
    EXTERNAL_CONTENT = "external_content"


class BoundaryEvent(BaseModel):
    """Base for every cross-domain event. No free text or prompts, only typed fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID = Field(default_factory=uuid4)
    origin_domain: Domain
    target_domain: Domain
    classification: DataClassification
    taint: Taint = Taint.NONE
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TradingIdeaProposed(BoundaryEvent):
    """A trading idea for the trading inbox. Trading decides; it never triggers execution."""

    kind: Literal["trading_idea_proposed"] = "trading_idea_proposed"
    summary: str = Field(max_length=2000)
    instruments: tuple[str, ...] = ()
    source_ref: str = Field(max_length=500)


class LifeTipProposed(BoundaryEvent):
    """A suggestion for the private inbox. Data flows into the private domain, never out."""

    kind: Literal["life_tip_proposed"] = "life_tip_proposed"
    summary: str = Field(max_length=2000)
    source_ref: str = Field(max_length=500)


class TaskMetadata(BoundaryEvent):
    """Metadata-only status report from a domain to core. Never carries content."""

    kind: Literal["task_metadata"] = "task_metadata"
    task_id: UUID
    state: str = Field(max_length=40)
    duration_ms: int = Field(ge=0)
    error_class: str | None = Field(default=None, max_length=80)
    # Model spend of the task so far (cloud only; local calls cost nothing). A number, no content.
    cost_eur: float = Field(default=0.0, ge=0, le=1000)


class ReelSubmitted(BoundaryEvent):
    """Core asks content to process one reel the owner submitted in the deck.

    Carries only the canonical reel reference and the task id under which content reports
    progress back as TaskMetadata. Never carries the reel's content.
    """

    kind: Literal["reel_submitted"] = "reel_submitted"
    task_id: UUID
    source_ref: str = Field(max_length=200)

    @field_validator("source_ref")
    @classmethod
    def _canonical(cls, value: str) -> str:
        if canonical_reel_url(value) != value:
            raise ValueError("source_ref must be a canonical reel URL")
        return value
