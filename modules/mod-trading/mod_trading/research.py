"""Signal research: does an idea beat the baseline before it may trade on paper?

Research agents (market researcher, earnings reviewer) may propose signals, but a proposal is
only a weight per bar: how much of `capital` to hold after that bar closes, between 0 and 1.
Whether it is worth anything is decided here, by plain arithmetic, never by a model:

- Both the candidate and the baseline (`TrendVolTarget`) are run as compounding weight curves
  on the same bars, with the same trading costs, without the paper account's absolute limits,
  so they compare signal against signal.
- The bars are split in two. The candidate must beat the baseline's Sharpe ratio by `margin`
  on the second half (out of sample) as well as on the whole, and must not draw down more
  than `max_extra_drawdown` beyond the baseline.

A signal that passes is still only eligible for paper trading through the normal order path;
nothing here places orders.
"""

from __future__ import annotations

import csv
import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from mod_trading.metrics import Performance, performance
from mod_trading.schemas import Bar
from mod_trading.strategies import TrendVolTarget
from trading_risk import AccountView

Curve = list[tuple[datetime, Decimal]]


class SignalError(Exception):
    pass


def weight_curve(bars: Sequence[Bar], weights: Sequence[float], cost_bps: float) -> Curve:
    """Equity of 1.0 that holds weights[i] of itself from bar i's close to bar i+1's close."""
    if len(weights) != len(bars):
        raise SignalError("one weight per bar is required")
    equity, held = 1.0, 0.0
    curve: Curve = [(bars[0].ts, Decimal(1))]
    for (prev, bar), weight in zip(itertools.pairwise(bars), weights, strict=False):
        equity *= 1 - abs(weight - held) * cost_bps / 10_000
        equity *= 1 + weight * (float(bar.close) / float(prev.close) - 1)
        held = weight
        curve.append((bar.ts, Decimal(repr(equity))))
    return curve


def baseline_weights(bars: Sequence[Bar], **params: object) -> list[float]:
    strategy = TrendVolTarget(bars[0].instrument, **params)  # type: ignore[arg-type]
    account = AccountView(equity=Decimal(1), day_start_equity=Decimal(1), peak_equity=Decimal(1))
    weights: list[float] = []
    for bar in bars:
        strategy.on_bar(bar, account)
        weights.append(strategy.target_weight() or 0.0)
    return weights


def read_signal(path: Path, bars: Sequence[Bar]) -> list[float]:
    """A signal CSV: `ts,weight`, one row per bar of the backtest, weights within [0, 1]."""
    by_ts: dict[datetime, float] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for number, row in enumerate(csv.DictReader(fh), start=2):
            try:
                ts, weight = datetime.fromisoformat(row["ts"]), float(row["weight"])
            except (KeyError, ValueError) as exc:
                raise SignalError(f"line {number}: {exc}") from exc
            if not 0.0 <= weight <= 1.0:
                raise SignalError(f"line {number}: weight outside [0, 1] (long only, no leverage)")
            by_ts[ts] = weight
    missing = [b.ts for b in bars if b.ts not in by_ts]
    if missing:
        raise SignalError(f"no weight for {len(missing)} bars, first {missing[0].isoformat()}")
    return [by_ts[b.ts] for b in bars]


@dataclass(frozen=True)
class Verdict:
    accepted: bool
    reasons: tuple[str, ...]
    candidate: Performance
    baseline: Performance
    candidate_oos: Performance
    baseline_oos: Performance


@dataclass(frozen=True)
class ResearchGate:
    bars_per_year: int
    cost_bps: float = 17.0
    margin: float = 0.10
    max_extra_drawdown: float = 0.05

    def evaluate(
        self, bars: Sequence[Bar], candidate: Sequence[float], baseline: Sequence[float]
    ) -> Verdict:
        if len(bars) < 40:
            raise SignalError("need at least 40 bars to judge a signal")
        half = len(bars) // 2

        def perf(weights: Sequence[float], start: int) -> Performance:
            curve = weight_curve(bars[start:], weights[start:], self.cost_bps)
            return performance(curve, self.bars_per_year)

        cand, base = perf(candidate, 0), perf(baseline, 0)
        cand_oos, base_oos = perf(candidate, half), perf(baseline, half)
        reasons: list[str] = []
        for label, c, b in (("whole period", cand, base), ("second half", cand_oos, base_oos)):
            if (c.sharpe or 0.0) < (b.sharpe or 0.0) + self.margin:
                reasons.append(f"{label}: Sharpe not at least {self.margin:.2f} above baseline")
        if cand.max_drawdown > base.max_drawdown + self.max_extra_drawdown:
            reasons.append("drawdown more than the allowed margin above baseline")
        return Verdict(not reasons, tuple(reasons), cand, base, cand_oos, base_oos)
