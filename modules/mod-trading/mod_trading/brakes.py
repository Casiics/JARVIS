"""Exposure brakes: reasons to hold less for a while, never a reason to buy.

A brake caps the weight a strategy may hold in one instrument until a point in time. Brakes
come from events that raise risk without telling which way prices go: earnings of the heavy
holdings inside an ETF, or (later) a burst of attention on social media. They can only lower a
position:

- `cap(instrument, at)` is the smallest `max_weight` of the brakes active at that moment, and
  1.0 without any. A strategy multiplies nothing by it; it takes `min(weight, cap)`.
- A brake that expires simply stops applying; nothing has to clear it.
- Whoever proposes a brake (a person, the earnings calendar, a research worker) cannot do more
  than that: there is no field that raises a weight or opens a position.

The book is a JSON file in the trading data directory, read again on every bar, so a brake
added from the command line reaches a running paper session at its next bar.

Earnings calendar brake (`earnings_brakes`): ETFs are market-cap weighted, so a handful of
companies move them. When holdings that together weigh at least `min_weight` of an ETF report
within the next `lead_days`, the ETF's cap drops to `max_weight` until the day after the last
of those reports. Holdings and weights come from `earnings/holdings.json`; report dates from a
calendar CSV (`symbol,date`), until a data source for them exists in n8n-trading.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter

from mod_trading.store import write_atomic

HOLDINGS = Path(__file__).resolve().parent / "earnings" / "holdings.json"
SYMBOL = r"^[A-Z0-9.\-]{1,20}$"


class BrakeError(Exception):
    pass


class Brake(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument: str = Field(pattern=SYMBOL)
    max_weight: float = Field(ge=0.0, lt=1.0)
    start: AwareDatetime
    until: AwareDatetime
    source: Literal["manual", "earnings", "social"]
    reason: str = Field(min_length=3, max_length=200)

    def active(self, at: datetime) -> bool:
        return self.start <= at < self.until


_BRAKES = TypeAdapter(list[Brake])


class BrakeBook:
    def __init__(self, path: Path) -> None:
        self.path = path

    def all(self) -> list[Brake]:
        if not self.path.exists():
            return []
        return _BRAKES.validate_json(self.path.read_text("utf-8"))

    def add(self, brakes: Iterable[Brake]) -> None:
        new = list(brakes)
        if any(b.until <= b.start for b in new):
            raise BrakeError("a brake must end after it starts")
        current = self.all()
        merged = current + [b for b in new if b not in current]
        write_atomic(self.path, json.dumps(_BRAKES.dump_python(merged, mode="json"), indent=1))

    def prune(self, now: datetime) -> int:
        """Drop expired brakes; returns how many."""
        current = self.all()
        kept = [b for b in current if b.until > now]
        if len(kept) != len(current):
            write_atomic(self.path, json.dumps(_BRAKES.dump_python(kept, mode="json"), indent=1))
        return len(current) - len(kept)

    def cap(self, instrument: str, at: datetime) -> float:
        """Fails closed: an unreadable book allows no exposure until it is fixed."""
        try:
            brakes = self.all()
        except (OSError, ValueError):
            return 0.0
        caps = [b.max_weight for b in brakes if b.instrument == instrument and b.active(at)]
        return min(caps, default=1.0)


class StaticBrakes:
    """Brakes held in memory, for backtests."""

    def __init__(self, brakes: Sequence[Brake]) -> None:
        self.brakes = tuple(brakes)

    def cap(self, instrument: str, at: datetime) -> float:
        caps = [b.max_weight for b in self.brakes if b.instrument == instrument and b.active(at)]
        return min(caps, default=1.0)


# --- earnings calendar --------------------------------------------------------------------------


class Holding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(pattern=SYMBOL)
    weight: float = Field(gt=0.0, lt=1.0)


class EtfHoldings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: date
    note: str = Field(max_length=300)
    etfs: dict[str, tuple[Holding, ...]]


def load_holdings(path: Path = HOLDINGS) -> EtfHoldings:
    return EtfHoldings.model_validate_json(path.read_text("utf-8"))


def read_calendar(path: Path) -> list[tuple[str, date]]:
    """`symbol,date` rows (ISO dates); the rest of the columns are ignored."""
    rows: list[tuple[str, date]] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for number, row in enumerate(csv.DictReader(fh), start=2):
            try:
                symbol, day = row["symbol"].strip().upper(), date.fromisoformat(row["date"])
            except (KeyError, ValueError, AttributeError) as exc:
                raise BrakeError(f"line {number}: {exc}") from exc
            rows.append((symbol, day))
    return rows


def earnings_brakes(
    holdings: EtfHoldings,
    calendar: Sequence[tuple[str, date]],
    *,
    lead_days: int = 1,
    min_weight: float = 0.10,
    max_weight: float = 0.5,
) -> list[Brake]:
    """One brake per ETF and cluster of heavy reports, from `lead_days` before to the day after.

    Reports are grouped per ETF into clusters of dates no more than `lead_days + 1` apart (the
    big technology names often report within a few days of each other), and a cluster brakes
    only if its holdings together weigh at least `min_weight`.
    """
    brakes: list[Brake] = []
    for etf, members in holdings.etfs.items():
        weight = {h.symbol: h.weight for h in members}
        reports = sorted((day, symbol) for symbol, day in calendar if symbol in weight)
        cluster: list[tuple[date, str]] = []
        for report in [*reports, None]:
            if report is not None and (
                not cluster or (report[0] - cluster[-1][0]).days <= lead_days + 1
            ):
                cluster.append(report)
                continue
            if cluster:
                total = sum(weight[s] for s in {s for _d, s in cluster})
                if total >= min_weight:
                    first, last = cluster[0][0], cluster[-1][0]
                    names = ", ".join(sorted({s for _d, s in cluster}))
                    brakes.append(
                        Brake(
                            instrument=etf,
                            max_weight=max_weight,
                            start=_midnight(first - timedelta(days=lead_days)),
                            until=_midnight(last + timedelta(days=2)),
                            source="earnings",
                            reason=f"earnings {names} ({total:.0%} of {etf})"[:200],
                        )
                    )
            cluster = [report] if report is not None else []
    return brakes


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=UTC)


def describe(brakes: Iterable[Brake]) -> list[str]:
    return [
        f"{b.instrument:6} cap {b.max_weight:.2f}  {b.start:%Y-%m-%d} to {b.until:%Y-%m-%d}  "
        f"[{b.source}] {b.reason}"
        for b in brakes
    ]
