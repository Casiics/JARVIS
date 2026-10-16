"""Performance figures for backtests, and the buy-and-hold benchmark to compare them with.

Plain arithmetic on an equity curve; no model and no I/O. Figures are floats because they are
reports, not money: account values stay Decimal everywhere else.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from mod_trading.schemas import Bar

SECONDS_PER_YEAR = 365.25 * 24 * 3600


@dataclass(frozen=True)
class Performance:
    total_return: float
    cagr: float | None
    volatility: float
    sharpe: float | None
    max_drawdown: float


def performance(curve: Sequence[tuple[datetime, Decimal]], bars_per_year: int) -> Performance:
    """Figures for an equity curve; volatility and Sharpe are annualised, risk-free rate 0."""
    if len(curve) < 2:
        raise ValueError("need at least two points")
    values = [float(v) for _, v in curve]
    returns = [b / a - 1 for a, b in itertools.pairwise(values)]
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / max(1, len(returns) - 1)
    volatility = math.sqrt(variance * bars_per_year)
    years = (curve[-1][0] - curve[0][0]).total_seconds() / SECONDS_PER_YEAR
    total = values[-1] / values[0] - 1
    peak, drawdown = values[0], 0.0
    for value in values:
        peak = max(peak, value)
        drawdown = max(drawdown, 1 - value / peak)
    return Performance(
        total_return=total,
        cagr=(1 + total) ** (1 / years) - 1 if years >= 1 else None,
        volatility=volatility,
        sharpe=mean * bars_per_year / volatility if volatility > 0 else None,
        max_drawdown=drawdown,
    )


def buy_and_hold(
    bars: Sequence[Bar], *, cash: Decimal, capital: Decimal, cost_bps: Decimal
) -> list[tuple[datetime, Decimal]]:
    """Equity of an account that buys `capital` worth at the first close and never trades again.

    Uses the same starting cash as the backtest, so both curves are on the same base, and pays
    `cost_bps` (spread, slippage and fee) once on the purchase.
    """
    if not bars:
        return []
    entry = bars[0].close * (1 + cost_bps / Decimal(10_000))
    quantity = (capital / entry).quantize(Decimal(1), ROUND_DOWN)
    rest = cash - quantity * entry
    return [(bar.ts, rest + quantity * bar.close) for bar in bars]


def bars_per_year(bars: Sequence[Bar]) -> int:
    """252 for daily bars, 52 weekly, 12 monthly; intraday: bars in 252 sessions of 6.5 h."""
    gaps = sorted((b.ts - a.ts).total_seconds() for a, b in itertools.pairwise(bars) if b.ts > a.ts)
    if not gaps:
        return 252
    median = gaps[len(gaps) // 2]
    if median < 20 * 3600:
        return max(252, round(252 * 23_400 / median))
    if median < 4 * 86_400:
        return 252
    return max(1, round(SECONDS_PER_YEAR / median))
