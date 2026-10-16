"""The trading inbox: ideas proposed by other domains (content, from reels).

An idea is a note for later review, never an order. It enters only through the boundary
policy, keeps its taint, and nothing in this module can reach the executor.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter

from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.events import BoundaryEvent, Taint, TradingIdeaProposed
from jarvis_core.policy.flows import check_flow
from mod_trading.store import write_atomic


class FlowRejected(Exception):
    pass


class Idea(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    idea_id: UUID = Field(default_factory=uuid4)
    event_id: UUID
    origin: Domain
    summary: str
    instruments: tuple[str, ...]
    source_ref: str
    taint: Taint
    received_at: AwareDatetime
    dismissed: bool = False


_IDEAS = TypeAdapter(list[Idea])


class TradingInbox:
    def __init__(self, path: Path) -> None:
        self.path = path

    def all(self) -> list[Idea]:
        if not self.path.exists():
            return []
        return _IDEAS.validate_json(self.path.read_text("utf-8"))

    def receive(self, event: BoundaryEvent, *, sender: Domain) -> Idea:
        if not isinstance(event, TradingIdeaProposed) or event.target_domain is not Domain.TRADING:
            raise FlowRejected("only trading ideas addressed to trading are accepted")
        decision = check_flow(event, caller_domain=sender)
        if not decision.allowed:
            raise FlowRejected(decision.reason)
        ideas = self.all()
        if any(i.event_id == event.event_id for i in ideas):
            raise FlowRejected("duplicate event")
        idea = Idea(
            event_id=event.event_id,
            origin=event.origin_domain,
            summary=event.summary,
            instruments=event.instruments,
            source_ref=event.source_ref,
            taint=event.taint,
            received_at=datetime.now(UTC),
        )
        self._save([*ideas, idea])
        return idea

    def dismiss(self, idea_id: UUID) -> None:
        ideas = [
            i.model_copy(update={"dismissed": True}) if i.idea_id == idea_id else i
            for i in self.all()
        ]
        self._save(ideas)

    def _save(self, ideas: list[Idea]) -> None:
        write_atomic(self.path, json.dumps(_IDEAS.dump_python(ideas, mode="json")))
