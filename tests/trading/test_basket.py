"""Several ETFs at once: one baseline per instrument, one account, one set of risk limits.

The basket is compared with holding the same instruments, and its correlations are shown,
because SPY and QQQ together diversify less than two names suggest.
"""

from __future__ import annotations

import math
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from mod_trading.__main__ import main
from mod_trading.marketdata import BasketFeed, LiveFeed, MarketDataError
from mod_trading.portfolio import (
    PortfolioError,
    aligned,
    basket_curve,
    correlations,
    split_instruments,
)
from mod_trading.schemas import Bar
from mod_trading.strategies import Basket, TrendVolTarget
from tests.trading.test_trend_vol import DAY0, account, daily, regimes, write_csv


def test_instrument_lists_are_parsed_strictly() -> None:
    assert split_instruments("SPY") == ("SPY",)
    assert split_instruments("SPY, QQQ") == ("SPY", "QQQ")
    for bad in ("SPY,,QQQ", "SPY,SPY", ""):
        with pytest.raises(PortfolioError):
            split_instruments(bad)


def test_bars_are_aligned_on_common_timestamps() -> None:
    spy, qqq = daily([1.0, 2.0, 3.0]), daily([10.0, 20.0, 30.0, 40.0], "QQQ")[1:]
    series = aligned([*spy, *qqq], ["SPY", "QQQ"])
    assert [b.ts for b in series["SPY"]] == [b.ts for b in series["QQQ"]]
    assert len(series["SPY"]) == 2
    with pytest.raises(PortfolioError, match="IWM"):
        aligned(spy, ["SPY", "IWM"])


def test_basket_return_is_the_mean_of_its_sleeves() -> None:
    ts = [DAY0 + timedelta(days=i) for i in range(3)]
    up = [(t, Decimal(v)) for t, v in zip(ts, ("1", "1.1", "1.21"), strict=True)]
    flat = [(t, Decimal(1)) for t in ts]
    curve = basket_curve({"A": up, "B": flat})
    assert [round(float(v), 6) for _, v in curve] == [1.0, 1.05, round(1.05 * 1.05, 6)]


def test_correlation_of_identical_and_opposite_moves() -> None:
    closes = [100 * math.exp(0.01 * math.sin(i)) for i in range(50)]
    mirror = [100 * 100 / c for c in closes]
    pairs = correlations(
        {"A": daily(closes, "A"), "B": daily(closes, "B"), "C": daily(mirror, "C")}
    )
    assert pairs[("A", "B")] == pytest.approx(1.0)
    assert pairs[("A", "C")] < -0.99


def test_basket_routes_each_bar_to_its_own_member() -> None:
    spy, qqq = (
        TrendVolTarget("SPY", trend=3, vol_window=2),
        TrendVolTarget("QQQ", trend=3, vol_window=2),
    )
    basket = Basket({"SPY": spy, "QQQ": qqq})
    for bar in [*daily([1.0, 1.1, 1.2]), *daily([5.0, 5.5], "QQQ")]:
        basket.on_bar(bar, account())
    assert spy.target_weight() is not None  # three SPY bars: warmed up
    assert qqq.target_weight() is None  # only two QQQ bars: QQQ never saw SPY's
    assert basket.on_bar(daily([1.0], "IWM")[0], account()) == []


def test_basket_backtest_compares_sleeves_and_reports_correlation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spy = write_csv(tmp_path / "spy.csv", daily(regimes(7)))
    qqq = write_csv(tmp_path / "qqq.csv", daily([c / 1.3 for c in regimes(11)], "QQQ"))
    code = main(
        ["backtest", "--bars", str(spy), str(qqq), "--instrument", "SPY,QQQ",
         "--strategy", "trend-vol", "--trend", "100", "--compare"]
    )  # fmt: skip
    assert code == 0
    out = capsys.readouterr().out
    assert "risk " not in out, out  # two sleeves of 2400 stay inside every limit
    names = {line.split()[0] for line in out.splitlines() if "%" in line}
    assert {"SPY/trend-vol", "QQQ/hold", "basket/trend-vol", "basket/hold"} <= names
    assert "SPY/QQQ:" in out


def test_sweep_takes_one_instrument(tmp_path: Path) -> None:
    spy = write_csv(tmp_path / "spy.csv", daily(regimes()))
    args = ["backtest", "--bars", str(spy), "--instrument", "SPY,QQQ", "--sweep"]
    assert main([*args, "--strategy", "trend-vol"]) == 2


class Source:
    def __init__(self, answers: dict[str, list[list[Bar] | Exception]]) -> None:
        self.answers = answers

    def bars(self, symbol: str, now: object) -> list[Bar]:
        answer = self.answers[symbol].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_basket_feed_polls_every_instrument_and_keeps_time_order() -> None:
    spy, qqq = daily([1.0, 2.0, 3.0]), daily([5.0, 6.0, 7.0], "QQQ")
    source = Source({"SPY": [spy[:1], spy[:3]], "QQQ": [qqq[:1], MarketDataError("down")]})
    errors: list[str] = []
    feeds = [
        LiveFeed(source, name, Decimal(4), on_error=errors.append, max_polls=1)
        for name in ("SPY", "QQQ")
    ]
    feed = BasketFeed(feeds, sleep=lambda _s: None, max_polls=1)
    assert len(feed.warmup()) == 2
    streamed = [(b.instrument, b.ts) for b, _q in feed.stream()]
    assert streamed == [("SPY", spy[1].ts), ("SPY", spy[2].ts)]
    assert errors == ["QQQ: down"]


def test_a_basket_larger_than_the_order_rate_is_refused(tmp_path: Path) -> None:
    spy = write_csv(tmp_path / "spy.csv", daily(regimes()))
    args = ["backtest", "--bars", str(spy), "--instrument", "SPY,QQQ,IWM,DIA"]
    assert main([*args, "--strategy", "trend-vol"]) == 3


def test_paper_live_runs_a_basket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from mod_trading import __main__ as cli

    class Live:
        def __init__(self, url: str, post: object) -> None:
            pass

        def bars(self, symbol: str, now: object) -> list[Bar]:
            return daily([100.0, 101.0], symbol)

    monkeypatch.setattr(cli, "McpBarSource", Live)
    data = ["--data-dir", str(tmp_path)]
    assert main([*data, "kill-switch", "release"]) == 0
    assert main([*data, "paper-live", "--instrument", "SPY,QQQ", "--once"]) == 0
    out = capsys.readouterr().out
    assert "SPY: 2 bars of history" in out and "QQQ: 2 bars of history" in out


def test_instruments_off_the_allowlist_are_compared_but_not_traded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spy = write_csv(tmp_path / "spy.csv", daily(regimes(7)))
    iwm = write_csv(tmp_path / "iwm.csv", daily([c / 2 for c in regimes(5)], "IWM"))
    code = main(
        ["backtest", "--bars", str(spy), str(iwm), "--instrument", "SPY,IWM",
         "--strategy", "trend-vol", "--trend", "100", "--compare"]
    )  # fmt: skip
    assert code == 0
    captured = capsys.readouterr()
    assert "IWM: not on the risk allowlist" in captured.err
    assert "risk " not in captured.out  # no IWM order was even proposed
    assert "IWM/trend-vol" in captured.out
