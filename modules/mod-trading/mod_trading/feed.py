"""Market data for paper trading and backtests: OHLC bars replayed from CSV.

CSV columns: ts (ISO 8601 with offset, the bar's close time), instrument, open, high, low,
close, volume (optional). Bars are replayed in time order; each becomes a quote around its close
with a fixed spread. Live quotes arrive later through n8n-trading.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from mod_trading.schemas import Bar, Quote

MAX_ROWS = 2_000_000


class FeedError(Exception):
    pass


def read_bars(path: Path) -> list[Bar]:
    bars: list[Bar] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for number, row in enumerate(csv.DictReader(fh), start=2):
            if number > MAX_ROWS:
                raise FeedError("too many rows")
            try:
                bars.append(
                    Bar(
                        instrument=row["instrument"],
                        ts=datetime.fromisoformat(row["ts"]),
                        open=Decimal(row["open"]),
                        high=Decimal(row["high"]),
                        low=Decimal(row["low"]),
                        close=Decimal(row["close"]),
                        volume=Decimal(row.get("volume") or 0),
                    )
                )
            except (KeyError, ValueError, InvalidOperation) as exc:
                raise FeedError(f"line {number}: {exc}") from exc
    bars.sort(key=lambda b: (b.ts, b.instrument))
    return bars


def quote_from_bar(bar: Bar, spread_bps: Decimal) -> Quote:
    half = bar.close * spread_bps / Decimal(20_000)
    return Quote(instrument=bar.instrument, bid=bar.close - half, ask=bar.close + half, ts=bar.ts)


def replay(bars: list[Bar], spread_bps: Decimal) -> Iterator[tuple[Bar, Quote]]:
    for bar in bars:
        yield bar, quote_from_bar(bar, spread_bps)
