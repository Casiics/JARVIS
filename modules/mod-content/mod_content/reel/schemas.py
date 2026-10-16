"""Typed objects of the reel pipeline (docs/architecture.md, section 6.2).

Everything derived from a reel is external content: it carries the `external_content` taint
and is treated as data, never as instructions.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from jarvis_core.contracts.classification import DataClassification
from jarvis_core.contracts.events import Taint

Line = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class TargetModule(StrEnum):
    SELF_EVOLUTION = "mod-self-evolution"
    TRADING = "mod-trading"
    CONTENT = "mod-content"
    LIFE_OS = "mod-life-os"
    NONE = "none"


class ReelMetadata(BaseModel):
    """metadata.json written by the fetch sandbox. Every field is untrusted."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    webpage_url: str = Field(max_length=500)
    caption: str = Field(default="", max_length=5000)
    uploader: str = Field(default="", max_length=200)
    upload_date: str | None = Field(default=None, pattern=r"^\d{8}$")
    duration_s: float = Field(ge=0, le=3600)


class TranscriptSegment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    text: str = Field(max_length=2000)


class Transcript(BaseModel):
    """transcript.json written by the Faster-Whisper sandbox job."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    language: str = Field(max_length=10)
    segments: tuple[TranscriptSegment, ...] = Field(max_length=2000)
    model: str = Field(max_length=100)

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments if s.text.strip())


class ExtractionDraft(BaseModel):
    """What the quarantined worker may return. It cannot set labels, links or references."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(min_length=1, max_length=1500)
    extracted_tools: tuple[Line, ...] = Field(default=(), max_length=20)
    actionable_steps: tuple[Line, ...] = Field(default=(), max_length=10)
    target_module: TargetModule
    utility_score: float = Field(ge=0.0, le=1.0)


class ReelKnowledgeArtifact(BaseModel):
    """A validated draft plus provenance, labels and hashes set by deterministic code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reel_id: str = Field(pattern=r"^[A-Za-z0-9_-]{5,40}$")
    source_ref: str = Field(max_length=500)
    task_id: UUID
    created_at: datetime
    classification: DataClassification = DataClassification.PUBLIC
    taint: Taint = Taint.EXTERNAL_CONTENT
    summary: str
    extracted_tools: tuple[str, ...]
    actionable_steps: tuple[str, ...]
    target_module: TargetModule
    utility_score: float
    injection_flags: tuple[str, ...] = ()
    media_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcript_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def needs_human_review(self) -> bool:
        return bool(self.injection_flags)
