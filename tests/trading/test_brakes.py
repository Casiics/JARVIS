"""Exposure brakes: earnings of an ETF's heavy holdings (and later social media) may only lower
the weight the baseline holds, for a limited time, and never open a position.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from mod_trading.__main__ import main
from mod_trading.brakes import (
    Brake,
    BrakeBook,
    BrakeError,
    EtfHoldings,
    StaticBrakes,
    earnings_brakes,
    load_holdings,
    read_calendar,
)
from mod_trading.schemas import Side
from mod_trading.strategies import TrendVolTarget
from tests.trading.test_trend_vol import account, daily, write_csv

NOW = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)


def brake(instrument: str = "QQQ", cap: float = 0.5, hours: float = 24, **kw: object) -> Brake:
    return Brake.model_validate(
        {
            "instrument": instrument,
            "max_weight": cap,
            "start": NOW,
            "until": NOW + timedelta(hours=hours),
            "source": "manual",
            "reason": "test brake",
            **kw,
        }
    )


def test_the_tightest_active_brake_wins_and_expired_ones_do_not_count(tmp_path: Path) -> None:
    book = BrakeBook(tmp_path / "brakes.json")
    assert book.cap("QQQ", NOW) == 1.0
    book.add([brake(cap=0.5), brake(cap=0.25, hours=1), brake("SPY", cap=0.0)])
    assert book.cap("QQQ", NOW) == 0.25
    assert book.cap("QQQ", NOW + timedelta(hours=2)) == 0.5
    assert book.cap("QQQ", NOW + timedelta(days=2)) == 1.0
    assert book.cap("IWM", NOW) == 1.0
    assert book.prune(NOW + timedelta(hours=2)) == 1


@pytest.mark.parametrize(
    "bad",
    [{"max_weight": 1.0}, {"max_weight": 1.5}, {"max_weight": -0.1}, {"instrument": "../x"}],
)
def test_a_brake_cannot_raise_exposure_or_name_anything_else(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        brake(**bad)  # type: ignore[arg-type]


def test_the_strategy_sells_down_to_the_cap() -> None:
    bars = daily([100.0 + i * 0.1 for i in range(30)], "QQQ")
    free = TrendVolTarget("QQQ", trend=10, vol_window=5, max_weight=1.0, target_vol=1.0)
    capped = TrendVolTarget(
        "QQQ",
        trend=10,
        vol_window=5,
        max_weight=1.0,
        target_vol=1.0,
        cap=StaticBrakes([brake(cap=0.25, start=bars[-1].ts, hours=1)]).cap,
    )
    for strategy in (free, capped):
        for bar in bars[:-1]:
            strategy.on_bar(bar, account())
    held = {"QQQ": Decimal(23)}  # roughly the full 2400 at 103
    assert free.on_bar(bars[-1], account(held)) == []
    intents = capped.on_bar(bars[-1], account(held))
    assert intents and all(i.side is Side.SELL for i in intents)
    assert sum(i.quantity for i in intents) == 23 - 5  # 2400 * 0.25 / 102.9 -> 5 shares


def holdings() -> EtfHoldings:
    return EtfHoldings.model_validate(
        {
            "as_of": "2025-09-30",
            "note": "test",
            "etfs": {
                "QQQ": [
                    {"symbol": "MSFT", "weight": 0.085},
                    {"symbol": "META", "weight": 0.037},
                    {"symbol": "AVGO", "weight": 0.055},
                ],
                "SPY": [{"symbol": "MSFT", "weight": 0.067}, {"symbol": "META", "weight": 0.029}],
                "IWM": [],
            },
        }
    )


def test_clustered_heavy_reports_brake_the_etf_they_move() -> None:
    calendar = [
        ("MSFT", date(2026, 10, 28)),
        ("META", date(2026, 10, 29)),
        ("AVGO", date(2026, 12, 10)),
    ]
    [qqq] = earnings_brakes(holdings(), calendar)
    assert qqq.instrument == "QQQ" and qqq.max_weight == 0.5
    assert qqq.start == datetime(2026, 10, 27, tzinfo=UTC)
    assert qqq.until == datetime(2026, 10, 31, tzinfo=UTC)
    assert "META, MSFT" in qqq.reason and "12%" in qqq.reason
    # AVGO alone weighs 5.5 % of QQQ, and MSFT plus META only 9.6 % of SPY: no brake.


def test_the_shipped_holdings_load_and_iwm_has_no_heavy_names() -> None:
    shipped = load_holdings()
    assert {"SPY", "QQQ", "IWM"} <= set(shipped.etfs)
    assert shipped.etfs["IWM"] == ()
    assert all(sum(h.weight for h in members) < 0.8 for members in shipped.etfs.values())


def test_calendar_rows_are_checked(tmp_path: Path) -> None:
    good = tmp_path / "cal.csv"
    good.write_text("symbol,date,time\nmsft,2026-10-28,amc\n")
    assert read_calendar(good) == [("MSFT", date(2026, 10, 28))]
    bad = tmp_path / "bad.csv"
    bad.write_text("symbol,date\nMSFT,next week\n")
    with pytest.raises(BrakeError, match="line 2"):
        read_calendar(bad)


def test_brakes_command_adds_and_lists(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = ["--data-dir", str(tmp_path)]
    assert main([*data, "brakes", "add", "qqq", "0.3", "--hours", "2", "--reason", "CPI"]) == 0
    assert "QQQ    cap 0.30" in capsys.readouterr().out
    assert main([*data, "brakes", "add", "QQQ", "1.2"]) == 2  # cannot raise exposure
    calendar = tmp_path / "cal.csv"
    soon = datetime.now(UTC).date() + timedelta(days=3)
    calendar.write_text(f"symbol,date\nMSFT,{soon}\nNVDA,{soon}\n")
    assert main([*data, "brakes", "earnings", "--calendar", str(calendar)]) == 0
    out = capsys.readouterr().out
    assert "[earnings]" in out and "QQQ" in out and "SPY" in out


def test_a_backtest_can_replay_an_earnings_calendar(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bars = daily([100.0 + i * 0.2 for i in range(260)])
    csv = write_csv(tmp_path / "spy.csv", bars)
    args = ["backtest", "--bars", str(csv), "--instrument", "SPY", "--strategy", "trend-vol",
            "--trend", "50"]  # fmt: skip
    assert main(args) == 0
    free = capsys.readouterr().out
    report_day = bars[200].ts.date()
    calendar = tmp_path / "cal.csv"
    calendar.write_text(f"symbol,date\nNVDA,{report_day}\nMSFT,{report_day}\n")
    assert main([*args, "--earnings-calendar", str(calendar)]) == 0
    braked = capsys.readouterr().out
    intents = [int(out.split("intents ")[1].split()[0]) for out in (free, braked)]
    assert intents[1] > intents[0]  # it sold down before the reports and bought back after


def test_an_unreadable_book_allows_no_exposure(tmp_path: Path) -> None:
    path = tmp_path / "brakes.json"
    path.write_text('[{"instrument": "QQQ", "max_weight": 9}]')
    assert BrakeBook(path).cap("SPY", NOW) == 0.0
