"""Review the trading inbox with the supervisor-worker loop.

Ideas arrive from content (reels) as tainted notes. For each open idea a local worker writes
a typed assessment, Tier 0b checks it deterministically, and a local supervisor does the
semantic QA (core's `SupervisorWorkerLoop`, at most four tries). The assessment is a note for
the owner: it never dismisses an idea, never places an order and never reaches the executor.
Ideas stay tainted, so anything the supervisor proposes is raised to at least tier C.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter

from jarvis_core.contracts.classification import DataClassification
from jarvis_core.contracts.task_token import TaskTokenClaims
from jarvis_core.orchestrator.loop import (
    Acceptance,
    RuntimeSupervisor,
    RuntimeWorker,
    SupervisorWorkerLoop,
    WorkOrder,
)
from jarvis_core.orchestrator.state_machine import TaskState
from mod_trading.inbox import Idea, TradingInbox
from mod_trading.store import write_atomic

INSTRUCTION = (
    "Assess this trading idea from an external reel for a small, long-only paper portfolio. "
    "Decide whether it is worth a backtest (research), worth watching (watch) or not worth "
    "more time (dismiss). Name only instruments the idea itself mentions. Be sober: most "
    "ideas from social media do not beat a simple trend-following baseline."
)
_TICKER = re.compile(r"^[A-Z0-9.\-]{1,15}$")


class Verdict(StrEnum):
    RESEARCH = "research"
    WATCH = "watch"
    DISMISS = "dismiss"


class IdeaAssessment(BaseModel):
    """The worker's answer, as Tier 0b accepts it."""

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    instruments: list[str] = Field(default_factory=list, max_length=5)
    rationale: str = Field(min_length=10, max_length=600)
    risks: list[str] = Field(default_factory=list, max_length=5)


class StoredAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    idea_id: UUID
    state: TaskState
    assessment: IdeaAssessment | None
    error_class: str | None
    attempts: int
    runtime: str
    reviewed_at: AwareDatetime


_STORED = TypeAdapter(list[StoredAssessment])


def acceptance_for(idea: Idea) -> Acceptance[IdeaAssessment]:
    known = {i.upper() for i in idea.instruments}

    def tickers(a: IdeaAssessment) -> str | None:
        bad = [i for i in a.instruments if not _TICKER.match(i)]
        return f"instruments must be plain tickers: {bad}" if bad else None

    def only_mentioned(a: IdeaAssessment) -> str | None:
        extra = [i for i in a.instruments if known and i.upper() not in known]
        return f"name only instruments from the idea, not {extra}" if extra else None

    def research_needs_instrument(a: IdeaAssessment) -> str | None:
        if a.verdict is Verdict.RESEARCH and not a.instruments:
            return "a research verdict needs at least one instrument to backtest"
        return None

    return Acceptance(
        IdeaAssessment,
        checks=(tickers, only_mentioned, research_needs_instrument),
        max_chars=4000,
    )


class AssessmentStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def all(self) -> list[StoredAssessment]:
        if not self.path.exists():
            return []
        return _STORED.validate_json(self.path.read_text("utf-8"))

    def latest(self) -> dict[UUID, StoredAssessment]:
        return {a.idea_id: a for a in sorted(self.all(), key=lambda a: a.reviewed_at)}

    def add(self, item: StoredAssessment) -> None:
        write_atomic(self.path, json.dumps(_STORED.dump_python([*self.all(), item], mode="json")))


@dataclass
class IdeaReviewer:
    loop: SupervisorWorkerLoop
    worker: RuntimeWorker
    supervisor: RuntimeSupervisor | None
    claims: Callable[[], TaskTokenClaims]
    runtime: str = "gateway"
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def review(self, idea: Idea) -> StoredAssessment:
        claims = self.claims()
        order = WorkOrder(
            claims=claims,
            trace_id=claims.task_id.hex,
            instruction=INSTRUCTION,
            # Ideas come from public reels; the label never goes below what content sent.
            label=DataClassification.INTERNAL,
            inputs=(
                ("idea_summary", idea.summary),
                ("instruments", ", ".join(idea.instruments) or "none named"),
            ),
        )
        outcome = self.loop.run(order, acceptance_for(idea), self.worker, self.supervisor)
        return StoredAssessment(
            idea_id=idea.idea_id,
            state=outcome.state,
            assessment=outcome.value,
            error_class=outcome.error_class,
            attempts=outcome.attempts,
            runtime=self.runtime,
            reviewed_at=self.clock(),
        )


def review_open_ideas(
    inbox: TradingInbox,
    store: AssessmentStore,
    reviewer: IdeaReviewer,
    *,
    limit: int = 10,
    again: bool = False,
) -> list[StoredAssessment]:
    """Assess open ideas that have no successful assessment yet (or all of them with again)."""
    done = {k for k, v in store.latest().items() if v.state is TaskState.DONE}
    todo = [i for i in inbox.all() if not i.dismissed and (again or i.idea_id not in done)]
    results = []
    for idea in todo[:limit]:
        result = reviewer.review(idea)
        store.add(result)
        results.append(result)
    return results
