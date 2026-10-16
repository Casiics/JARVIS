"""Telemetry for the command deck. Metadata only: never prompts, outputs or raw data.

Mirrored by the Zod schema in apps/deck/src/schemas.ts; change both together.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from jarvis_core.contracts.classification import Domain


class TelemetryState(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    GATE_CHECK = "gate_check"
    SUPERVISOR_QA = "supervisor_qa"
    AWAITING_ACTION = "awaiting_action"
    EXECUTING = "executing"
    DONE = "done"
    ESCALATED = "escalated"


class TelemetryEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    task_id: UUID
    domain: Domain
    node: str = Field(max_length=80, pattern=r"^[a-z0-9_.:-]+$")
    state: TelemetryState
    duration_ms: int = Field(default=0, ge=0)
    cost_eur: float = Field(default=0.0, ge=0)
    model_class: str | None = Field(default=None, max_length=40, pattern=r"^[a-z0-9_-]+$")
    error_class: str | None = Field(default=None, max_length=80, pattern=r"^[a-z0-9_.:-]+$")
