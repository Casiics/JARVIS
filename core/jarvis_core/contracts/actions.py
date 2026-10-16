"""Requests for actions with an external effect. Only the action gate executes them."""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.events import Taint


class ApprovalTier(StrEnum):
    """A: read-only internal, B: reversible private, C: externally visible, D: money or main."""

    A = "A"
    B = "B"
    C = "C"
    D = "D"


class ActionRequest(BaseModel):
    """An intended external effect, proposed by a plan and decided by deterministic code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    domain: Domain
    tool: str = Field(max_length=100)
    parameters: dict[str, Any] = Field(default_factory=dict)
    tier: ApprovalTier
    taint: Taint = Taint.NONE
    rationale: str = Field(max_length=2000)
