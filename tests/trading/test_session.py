"""Backtest and paper sessions run through the same gate, risk engine and kill switch."""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from mod_trading.__main__ import main
from mod_trading.feed import FeedError, read_bars, replay
from mod_trading.schemas import Bar, OrderIntent, Side
from mod_trading.session import SessionReport, run_session
from mod_trading.strategies import SmaCrossover, Strategy
from tests.trading.helpers import T0, Rig, claims
from trading_risk import AccountView


def wave(n: int, *, base: float = 100, amp: float = 10, period: int = 60) -> list[Bar]:
    bars = []
    for i in range(n):
        close = Decimal(str(round(base + amp * math.sin(2 * math.pi * i / period), 2)))
        bars.append(
            Bar(
                instrument="SPY",
                ts=T0 + timedelta(minutes=i),
                open=close,
                high=close + 1,
                low=close - 1,
                close=close,
            )
        )
    return bars


def run(rig: Rig, bars: list[Bar], strategy: Strategy) -> SessionReport:
    def set_time(moment: datetime) -> None:
        rig.clock.now = moment

    return run_session(replay(bars, Decimal(4)), strategy, rig.executor, claims, set_time)


def test_crossover_buys_and_sells_within_limits(tmp_path: Path) -> None:
    rig = Rig(tmp_path, max_orders_per_day=50)
    report = run(rig, wave(300), SmaCrossover("SPY", fast=5, slow=20, notional=Decimal(900)))
    assert report.outcomes["filled"] >= 4
    assert report.violations == {}
    assert report.kill_switch_trips == 0
    sides = [f.side.value for f in rig.broker.state.fills]
    assert sides[0] == "buy"
    assert all(a != b for a, b in itertools.pairwise(sides)), "long-only alternates"


@dataclass
class AlwaysBuy:
    """Proposes one share every bar, so any order after the trip would show up as a fill."""

    name: str = "always_buy"

    def on_bar(self, bar: Bar, account: AccountView) -> list[OrderIntent]:
        return [
            OrderIntent(
                strategy=self.name,
                instrument=bar.instrument,
                side=Side.BUY,
                quantity=Decimal(1),
                reason="every bar",
            )
        ]


def test_crash_trips_the_switch_and_stops_trading(tmp_path: Path) -> None:
    rig = Rig(tmp_path, max_orders_per_day=500, max_orders_per_minute=3)
    crash = [
        Bar(instrument="SPY", ts=T0 + timedelta(minutes=k), open=p, high=p, low=p, close=p)
        for k, p in enumerate(Decimal(100 - 2 * k) for k in range(30))
    ]
    report = run(rig, crash, AlwaysBuy())
    assert report.kill_switch_trips == 1
    since = rig.kill_switch.state().since
    assert since is not None and rig.kill_switch.engaged()
    assert any(f.ts <= since for f in rig.broker.state.fills)
    assert [f for f in rig.broker.state.fills if f.ts > since] == []
    assert report.violations["kill_switch"] > 0


def test_backtest_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    csv = tmp_path / "bars.csv"
    rows = ["ts,instrument,open,high,low,close,volume"]
    rows += [f"{b.ts.isoformat()},SPY,{b.open},{b.high},{b.low},{b.close},100" for b in wave(240)]
    csv.write_text("\n".join(rows))
    code = main(
        [
            "backtest",
            "--bars",
            str(csv),
            "--instrument",
            "SPY",
            "--fast",
            "5",
            "--slow",
            "20",
            "--notional",
            "500",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "bars 240" in out and "filled" in out and "kill switch trips 0" in out


def test_paper_cli_refuses_until_the_switch_is_released(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    csv = tmp_path / "bars.csv"
    csv.write_text(
        "ts,instrument,open,high,low,close\n"
        + "\n".join(f"{b.ts.isoformat()},SPY,{b.open},{b.high},{b.low},{b.close}" for b in wave(40))
    )
    data = ["--data-dir", str(tmp_path / "data")]
    paper = [
        *data,
        "paper",
        "--bars",
        str(csv),
        "--instrument",
        "SPY",
        "--fast",
        "3",
        "--slow",
        "10",
    ]
    assert main(paper) == 3
    assert main([*data, "kill-switch", "release"]) == 0
    assert main(paper) == 0
    assert main([*data, "status"]) == 0
    out = capsys.readouterr().out
    assert "kill switch released" in out
    assert (tmp_path / "data" / "audit.jsonl").exists()


def test_feed_rejects_bad_rows(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("ts,instrument,open,high,low,close\n2026-10-15T09:00:00+00:00,SPY,1,0.5,1,1\n")
    with pytest.raises(FeedError):
        read_bars(path)
    path.write_text("ts,instrument,open,high,low,close\n2026-10-15T09:00:00,SPY,1,1,1,1\n")
    with pytest.raises(FeedError):
        read_bars(path)
