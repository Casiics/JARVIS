"""Several instruments as one portfolio: aligned bars, a combined curve and correlations.

The basket gives every instrument an equal sleeve of capital and rebalances the sleeves to
equal size every bar, so its return per bar is the mean of the sleeves' returns. That keeps
the comparison honest: the baseline basket and the buy-and-hold basket hold the same
instruments on the same bars with the same costs.

Correlations are shown next to the figures because they decide how much a basket helps: SPY
and QQQ move together most of the time, so holding both is less diversified than it looks.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from mod_trading.research import Curve, weight_curve
from mod_trading.schemas import Bar


class PortfolioError(Exception):
    pass


def split_instruments(spec: str) -> tuple[str, ...]:
    """`SPY,QQQ` -> ('SPY', 'QQQ'); order kept, duplicates and blanks refused."""
    names = tuple(part.strip() for part in spec.split(","))
    if not names or any(not n for n in names) or len(set(names)) != len(names):
        raise PortfolioError(f"invalid instrument list: {spec!r}")
    return names


def aligned(bars: Sequence[Bar], instruments: Sequence[str]) -> dict[str, list[Bar]]:
    """Each instrument's bars, restricted to the timestamps every instrument has."""
    by_instrument: dict[str, dict[datetime, Bar]] = {name: {} for name in instruments}
    for bar in bars:
        if bar.instrument in by_instrument:
            by_instrument[bar.instrument][bar.ts] = bar
    missing = [name for name, series in by_instrument.items() if not series]
    if missing:
        raise PortfolioError(f"no bars for {', '.join(missing)}")
    common = set.intersection(*(set(series) for series in by_instrument.values()))
    if len(common) < 2:
        raise PortfolioError("the instruments share fewer than two bars")
    times = sorted(common)
    return {name: [series[ts] for ts in times] for name, series in by_instrument.items()}


def basket_curve(curves: Mapping[str, Curve]) -> Curve:
    """Equal sleeves, rebalanced each bar: the mean of the sleeves' returns per bar."""
    if not curves:
        raise PortfolioError("no sleeves")
    series = list(curves.values())
    length = len(series[0])
    if any(len(c) != length for c in series):
        raise PortfolioError("sleeves are not aligned")
    equity = 1.0
    out: Curve = [(series[0][0][0], Decimal(1))]
    for i in range(1, length):
        step = sum(float(c[i][1]) / float(c[i - 1][1]) - 1 for c in series) / len(series)
        equity *= 1 + step
        out.append((series[0][i][0], Decimal(repr(equity))))
    return out


def basket_of_weights(
    sleeves: Mapping[str, tuple[Sequence[Bar], Sequence[float]]], cost_bps: float
) -> Curve:
    return basket_curve(
        {name: weight_curve(bars, weights, cost_bps) for name, (bars, weights) in sleeves.items()}
    )


def correlations(series: Mapping[str, Sequence[Bar]]) -> dict[tuple[str, str], float]:
    """Pearson correlation of per-bar returns for every pair of aligned instruments."""
    returns = {
        name: [float(b.close) / float(a.close) - 1 for a, b in itertools.pairwise(bars)]
        for name, bars in series.items()
    }
    out: dict[tuple[str, str], float] = {}
    for left, right in itertools.combinations(returns, 2):
        out[(left, right)] = _pearson(returns[left], returns[right])
    return out


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = len(xs)
    if n != len(ys) or n < 2:
        raise PortfolioError("need two aligned series of at least two returns")
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else 0.0
