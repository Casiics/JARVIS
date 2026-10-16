"""Boundary events: the only way data crosses from one domain to another."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from jarvis_core.contracts.classification import DataClassification, Domain


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
