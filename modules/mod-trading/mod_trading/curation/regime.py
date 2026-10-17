"""Stage 1 source of the trading domain: market regime snapshots of the focus instruments.

Plain arithmetic on recorded daily bars (`python -m mod_trading history --out <dir>/QQQ.csv`)
turns into one snapshot per instrument: trend against the 50 and 200 day averages, realised
volatility, distance from the one-year high and the 20 day return, plus relative strength of
QQQ and IWM against SPY. Research agents read these as context; nothing here is a signal.

Each instrument is a snapshot subject, so a newer day replaces yesterday's entry instead of
piling up. Snapshots are INTERNAL (derived from public prices) and untainted (numbers we
computed, no third-party text).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterator, Sequence
from datetime import datetime
from pathlib import Path

from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.curation.models import CollectedItem
from jarvis_core.curation.similarity import fingerprint
from mod_trading.curation.sources import WATCHLIST
from mod_trading.feed import FeedError, read_bars
from mod_trading.schemas import Bar

MIN_BARS = 60
SIGNAL = 0.9
BENCHMARK = "SPY"


def regime_items(daily_dir: Path, watchlist: frozenset[str] = WATCHLIST) -> Iterator[CollectedItem]:
    closes = _closes(daily_dir, watchlist)
    for instrument in sorted(closes):
        series = closes[instrument]
        if len(series) >= MIN_BARS:
            yield regime_item(instrument, series, closes.get(BENCHMARK))


def regime_item(
    instrument: str,
    series: Sequence[tuple[datetime, float]],
    benchmark: Sequence[tuple[datetime, float]] | None = None,
) -> CollectedItem:
    as_of, last = series[-1]
    prices = [p for _, p in series]
    facts = [f"close {last:.2f} on {as_of.date().isoformat()}"]
    trend = []
    for window in (50, 200):
        if len(prices) >= window:
            avg = sum(prices[-window:]) / window
            side = "above" if last >= avg else "below"
            trend.append(side)
            facts.append(f"{side} the {window} day average ({_pct(last / avg - 1)})")
    vol = _realised_vol(prices[-21:])
    facts.append(f"20 day realised volatility {vol * 100:.1f}% annualised")
    high = max(prices[-252:])
    facts.append(f"{_pct(last / high - 1)} from the one-year high")
    ret = prices[-1] / prices[-21] - 1
    facts.append(f"20 day return {_pct(ret)}")
    if benchmark and instrument != BENCHMARK and len(benchmark) >= 21:
        bench = {ts.date(): p for ts, p in benchmark}
        start, end = series[-21][0].date(), as_of.date()
        if start in bench and end in bench:
            rel = ret - (bench[end] / bench[start] - 1)
            facts.append(f"20 day return vs {BENCHMARK}: {_pct(rel)}")
    state = (
        "uptrend"
        if trend and all(t == "above" for t in trend)
        else "downtrend"
        if trend and all(t == "below" for t in trend)
        else "mixed trend"
    )
    summary = f"{instrument} market regime as of {as_of.date().isoformat()}: {state}, " + (
        f"volatility {vol * 100:.1f}%, 20 day return {_pct(ret)}."
    )
    return CollectedItem(
        item_id=f"regime:{instrument.casefold()}:{as_of.date().isoformat()}",
        domain=Domain.TRADING,
        classification=DataClassification.INTERNAL,
        source_ref=f"bars:{instrument}:daily:{as_of.date().isoformat()}",
        collected_at=as_of,
        title=f"{instrument} market regime",
        summary=summary,
        facts=tuple(facts),
        tags=(instrument.casefold(), "regime"),
        signal=SIGNAL,
        content_hash=fingerprint(summary, *facts),
        subject=f"regime:{instrument.casefold()}",
    )


def _closes(daily_dir: Path, watchlist: frozenset[str]) -> dict[str, list[tuple[datetime, float]]]:
    """Daily closes per watchlist instrument, one per date, from every CSV in the directory."""
    by_day: dict[str, dict[object, tuple[datetime, float]]] = {}
    if not daily_dir.is_dir():
        return {}
    for path in sorted(daily_dir.glob("*.csv")):
        if path.is_symlink():
            continue
        try:
            bars: list[Bar] = read_bars(path)
        except (FeedError, OSError):
            continue  # one broken file must not stop the other instruments
        for bar in bars:
            if bar.instrument in watchlist:
                by_day.setdefault(bar.instrument, {})[bar.ts.date()] = (bar.ts, float(bar.close))
    return {i: sorted(days.values()) for i, days in by_day.items()}


def _realised_vol(prices: Sequence[float]) -> float:
    returns = [math.log(b / a) for a, b in itertools.pairwise(prices)]
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(var) * math.sqrt(252)


def _pct(value: float) -> str:
    return f"{value * 100:+.1f}%"
