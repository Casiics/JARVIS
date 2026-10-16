"""Day-start and peak equity plus today's order times: the state the risk engine needs.

Kept apart from the broker so a live broker later feeds the same bookkeeping. The trading day
rolls over at midnight in the configured time zone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
from decimal import Decimal
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict

from mod_trading.store import write_atomic


class TrackerState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: date | None = None
    day_start_equity: Decimal | None = None
    peak_equity: Decimal | None = None
    orders_sent: list[AwareDatetime] = []


@dataclass
class EquityTracker:
    tz: tzinfo
    state_path: Path | None = None
    state: TrackerState = field(init=False)

    def __post_init__(self) -> None:
        if self.state_path is not None and self.state_path.exists():
            self.state = TrackerState.model_validate_json(self.state_path.read_text("utf-8"))
        else:
            self.state = TrackerState()

    def observe(self, equity: Decimal, now: datetime) -> None:
        today = now.astimezone(self.tz).date()
        if self.state.day != today:
            self.state.day = today
            self.state.day_start_equity = equity
            self.state.orders_sent = []
        self.state.peak_equity = max(self.state.peak_equity or equity, equity)
        self._save()

    def record_order(self, now: datetime) -> None:
        self.state.orders_sent.append(now)
        self._save()

    def _save(self) -> None:
        if self.state_path is not None:
            write_atomic(self.state_path, json.dumps(self.state.model_dump(mode="json")))
