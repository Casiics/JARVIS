"""Boundary events: the only way data crosses from one domain to another."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

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


class AuditAnchor(BoundaryEvent):
    """A domain's local audit chain head, anchored in core's central audit log.

    Domain processes keep their own hash-chained audit file (they cannot reach pg-audit).
    Sending the head regularly to core means a later rewrite, truncation or deletion of that
    file no longer goes unnoticed: the anchored (entries, head_hash) pairs in core's log must
    still match the file. Hashes and counts only, never entries.
    """

    kind: Literal["audit_anchor"] = "audit_anchor"
    chain: str = Field(pattern=r"^[a-z][a-z0-9-]{0,39}$", description="name of the local chain")
    entries: int = Field(ge=0)
    head_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReelSubmitted(BoundaryEvent):
    """Core asks content to process one reel the owner submitted in the deck.

    Carries only the canonical reel reference, the task id under which content reports
    progress back as TaskMetadata, and the signed task token. Never carries the reel's content.
    """

    kind: Literal["reel_submitted"] = "reel_submitted"
    task_id: UUID
    source_ref: str = Field(max_length=200)
    # Ed25519 task token from core's task router, scoped to this task. The worker checks it
    # with core's public key; it cannot mint tokens of its own.
    task_token: str | None = Field(default=None, max_length=4096)

    @field_validator("source_ref")
    @classmethod
    def _canonical(cls, value: str) -> str:
        if canonical_reel_url(value) != value:
            raise ValueError("source_ref must be a canonical reel URL")
        return value


class Job(StrEnum):
    """The predefined jobs core may start in a domain. A name, never a prompt or parameters.

    Each job belongs to exactly one domain (`JOB_DOMAIN`); the domain decides how to run it
    and keeps every input and output to itself.
    """

    PRIVATE_DAILY_BRIEFING = "private.daily_briefing"
    TRADING_PAPER_CHECK = "trading.paper_check"


JOB_DOMAIN: dict[Job, Domain] = {
    Job.PRIVATE_DAILY_BRIEFING: Domain.PRIVATE,
    Job.TRADING_PAPER_CHECK: Domain.TRADING,
}


class TaskAssigned(BoundaryEvent):
    """Core asks a domain to run one of its predefined jobs.

    Carries the job's name, the task id under which the domain reports back as TaskMetadata,
    and (once core signs tokens) the task token. No content travels in either direction: the
    domain reads its own data and answers with metadata only.
    """

    kind: Literal["task_assigned"] = "task_assigned"
    task_id: UUID
    job: Job
    task_token: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def _job_matches_domain(self) -> TaskAssigned:
        if JOB_DOMAIN[self.job] is not self.target_domain:
            raise ValueError(f"job {self.job.value} does not run in {self.target_domain.value}")
        return self


BriefId = Annotated[str, StringConstraints(pattern=r"^br:[0-9a-f]{24}$")]
ShortLine = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class ContextReviewRequested(BoundaryEvent):
    """Content asks the owner, through the deck, whether a curated item may enter its context.

    The one boundary event that carries content to core, so it is tightly bounded: public or
    internal material only (enforced by the flow policy), short typed fields, no free prompt.
    Everything in it is external content and the deck renders it as plain text.
    """

    kind: Literal["context_review_requested"] = "context_review_requested"
    brief_id: BriefId
    category: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    proposal: Literal["store", "merge", "discard"]
    confidence: float = Field(ge=0.0, le=1.0)
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1500)
    facts: tuple[ShortLine, ...] = Field(default=(), max_length=10)
    reasons: tuple[ShortLine, ...] = Field(default=(), max_length=8)
    flags: tuple[ShortLine, ...] = Field(default=(), max_length=10)
    source_refs: tuple[Annotated[str, StringConstraints(max_length=500)], ...] = Field(
        default=(), max_length=10
    )


class ContextReviewDecided(BoundaryEvent):
    """The owner's answer from the deck. Content applies it; core never touches the store."""

    kind: Literal["context_review_decided"] = "context_review_decided"
    brief_id: BriefId
    accept: bool
    decided_by: str = Field(max_length=200)
    note: str = Field(default="", max_length=300)
