"""The baseline strategy: trend filter plus volatility target on one instrument, paper only.

It is tested through the same session, gate, risk engine and kill switch it would trade under,
and compared with buying and holding the same capital.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from mod_trading.__main__ import main
from mod_trading.metrics import bars_per_year, buy_and_hold, performance
from mod_trading.schemas import Bar, Side
from mod_trading.strategies import TrendVolTarget
from trading_risk import AccountView

DAY0 = datetime(2023, 1, 3, 21, 0, tzinfo=UTC)


def daily(closes: list[float], instrument: str = "SPY") -> list[Bar]:
    bars = []
    for i, close in enumerate(closes):
        c = Decimal(str(round(close, 2)))
        bars.append(
            Bar(instrument=instrument, ts=DAY0 + timedelta(days=i), open=c, high=c, low=c, close=c)
        )
    return bars


def regimes(seed: int = 7) -> list[float]:
    """A calm bull market, a crash, a choppy bottom and a recovery: about three years."""
    rng = random.Random(seed)  # noqa: S311 - reproducible test data
    price, closes = 400.0, []
    for drift, vol, days in ((0.0006, 0.008, 300), (-0.004, 0.03, 90), (0.0, 0.02, 120),
                             (0.0008, 0.01, 280)):  # fmt: skip
        for _ in range(days):
            price *= math.exp(drift + vol * rng.gauss(0, 1))
            closes.append(price)
    return closes


def account(positions: dict[str, Decimal] | None = None) -> AccountView:
    return AccountView(
        equity=Decimal(10_000),
        day_start_equity=Decimal(10_000),
        peak_equity=Decimal(10_000),
        positions=positions or {},
    )


def feed(strategy: TrendVolTarget, bars: list[Bar], held: Decimal = Decimal(0)) -> list[object]:
    out: list[object] = []
    for bar in bars:
        out = list(strategy.on_bar(bar, account({"SPY": held} if held else None)))
    return out


def test_it_waits_for_the_trend_window() -> None:
    strategy = TrendVolTarget("SPY", trend=50, vol_window=10)
    for bar in daily([100 + i for i in range(49)]):
        assert strategy.on_bar(bar, account()) == []
    assert strategy.target_weight() is None


def test_calm_uptrend_gets_the_full_position_in_split_orders() -> None:
    strategy = TrendVolTarget("SPY", trend=50, vol_window=10, capital=Decimal(2400))
    intents = feed(strategy, daily([100 * 1.001**i for i in range(50)]))
    assert strategy.target_weight() == 1.0
    assert len(intents) == 3  # 2400 at ~105 = 22 shares, at most 1000 per order
    assert all(i.side is Side.BUY and i.quantity * Decimal(106) <= 1000 for i in intents)  # type: ignore[attr-defined]
    assert sum(i.quantity for i in intents) == 22  # type: ignore[attr-defined]


def test_volatile_uptrend_gets_a_smaller_position() -> None:
    rng = random.Random(1)  # noqa: S311 - reproducible test data
    closes = [100 * math.exp(0.003 * i + 0.03 * rng.gauss(0, 1)) for i in range(60)]
    closes[-1] = max(closes) * 1.01  # still above its average
    strategy = TrendVolTarget("SPY", trend=50, vol_window=10, target_vol=0.10)
    feed(strategy, daily(closes))
    weight = strategy.target_weight()
    assert weight is not None and 0 < weight < 0.5


def test_broken_trend_sells_everything() -> None:
    strategy = TrendVolTarget("SPY", trend=50, vol_window=10)
    closes = [100.0] * 49 + [90.0]
    intents = feed(strategy, daily(closes), held=Decimal(20))
    assert strategy.target_weight() == 0.0
    assert [i.side for i in intents] == [Side.SELL, Side.SELL]  # type: ignore[attr-defined]
    assert sum(i.quantity for i in intents) == 20  # type: ignore[attr-defined]


def test_small_gaps_are_not_traded() -> None:
    strategy = TrendVolTarget("SPY", trend=50, vol_window=10, band=Decimal("0.10"))
    intents = feed(strategy, daily([100 * 1.001**i for i in range(50)]), held=Decimal(21))
    assert intents == []


@pytest.mark.parametrize(
    "kwargs", [{"trend": 20, "vol_window": 20}, {"target_vol": 0.0}, {"max_weight": 1.5}]
)
def test_nonsense_parameters_are_refused(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        TrendVolTarget("SPY", **kwargs)  # type: ignore[arg-type]


def test_metrics_on_a_known_curve() -> None:
    curve = [
        (DAY0 + timedelta(days=365 * i // 2), Decimal(v)) for i, v in enumerate([100, 120, 90, 121])
    ]
    p = performance(curve, 2)
    assert p.total_return == pytest.approx(0.21)
    assert p.max_drawdown == pytest.approx(0.25)
    assert p.cagr == pytest.approx(1.21 ** (1 / 1.5) - 1, rel=1e-2)  # 365.25-day years


def test_buy_and_hold_uses_the_same_cash_base() -> None:
    curve = buy_and_hold(
        daily([100.0, 110.0]), cash=Decimal(10_000), capital=Decimal(2400), cost_bps=Decimal(0)
    )
    assert [v for _, v in curve] == [Decimal(10_000), Decimal(10_240)]


def test_bar_frequency_is_inferred() -> None:
    assert bars_per_year(daily([1.0, 2.0, 3.0])) == 252
    minutes = [
        Bar(instrument="SPY", ts=DAY0 + timedelta(minutes=i), open=Decimal(1), high=Decimal(1),
            low=Decimal(1), close=Decimal(1))
        for i in range(3)
    ]  # fmt: skip
    assert bars_per_year(minutes) == 252 * 390
    monthly = [
        Bar(instrument="SPY", ts=DAY0 + timedelta(days=30.4 * i), open=Decimal(1),
            high=Decimal(1), low=Decimal(1), close=Decimal(1))
        for i in range(4)
    ]  # fmt: skip
    assert bars_per_year(monthly) == 12


def write_csv(path: Path, bars: list[Bar]) -> Path:
    lines = ["ts,instrument,open,high,low,close"]
    lines += [
        f"{b.ts.isoformat()},{b.instrument},{b.open},{b.high},{b.low},{b.close}" for b in bars
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


def test_backtest_against_buy_and_hold_stays_within_limits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    csv = write_csv(tmp_path / "spy-daily.csv", daily(regimes()))
    code = main(
        ["backtest", "--bars", str(csv), "--instrument", "SPY", "--strategy", "trend-vol",
         "--trend", "100", "--compare"]
    )  # fmt: skip
    assert code == 0
    out = capsys.readouterr().out
    assert "risk " not in out, out  # no order was refused by the risk engine
    assert "kill switch trips 0" in out
    rows = {line.split()[0]: line.split()[1:] for line in out.splitlines()[-2:]}
    assert set(rows) == {"trend_vol_target", "buy_and_hold"}
    # Through the crash the trend filter is out of the market for most of the fall.
    drawdown = {name: float(cols[-1].rstrip("%")) for name, cols in rows.items()}
    assert drawdown["trend_vol_target"] < drawdown["buy_and_hold"]


def test_sweep_shows_the_grid_next_to_buy_and_hold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    csv = write_csv(tmp_path / "spy-daily.csv", daily(regimes()))
    args = ["backtest", "--bars", str(csv), "--instrument", "SPY", "--strategy", "trend-vol"]
    assert main([*args, "--sweep"]) == 0
    lines = capsys.readouterr().out.splitlines()
    names = [line.split()[0] for line in lines[2:]]
    assert len(names) == 13 and names[-1] == "buy_and_hold"
    assert "*200/0.10" in names  # the defaults are marked
    assert not any(name.endswith("!") for name in names)  # no run hit the risk limits
