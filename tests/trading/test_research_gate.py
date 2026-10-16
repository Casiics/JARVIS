"""The research gate: a proposed signal must beat the baseline before it may trade on paper."""

from __future__ import annotations

import itertools
from decimal import Decimal
from pathlib import Path

import pytest

from mod_trading.__main__ import main
from mod_trading.research import (
    ResearchGate,
    SignalError,
    baseline_weights,
    read_signal,
    weight_curve,
)
from tests.trading.test_trend_vol import daily, regimes, write_csv

PARAMS = {"trend": 100, "vol_window": 20}


def test_weights_apply_from_one_close_to_the_next() -> None:
    bars = daily([100.0, 110.0, 99.0])
    curve = weight_curve(bars, [0.5, 1.0, 0.0], cost_bps=0)
    # Half invested for +10 %, then fully invested for -10 %.
    assert [round(float(v), 6) for _, v in curve] == [1.0, 1.05, 0.945]


def test_costs_are_charged_on_weight_changes() -> None:
    bars = daily([100.0, 100.0, 100.0])
    curve = weight_curve(bars, [1.0, 0.0, 0.0], cost_bps=100)
    assert round(float(curve[-1][1]), 6) == round(0.99 * 0.99, 6)


def write_signal(path: Path, bars: list, weights: list[float]) -> Path:  # type: ignore[type-arg]
    lines = ["ts,weight"] + [f"{b.ts.isoformat()},{w}" for b, w in zip(bars, weights, strict=True)]
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.mark.parametrize(
    ("weights", "message"),
    [([0.5, 1.5, 0.0], "outside"), ([0.5, -0.1, 0.0], "outside"), ([0.5, 0.5], "no weight")],
)
def test_bad_signals_are_refused(tmp_path: Path, weights: list[float], message: str) -> None:
    bars = daily([100.0, 101.0, 102.0])
    path = write_signal(tmp_path / "s.csv", bars[: len(weights)], weights)
    with pytest.raises(SignalError, match=message):
        read_signal(path, bars)


def gate() -> ResearchGate:
    return ResearchGate(bars_per_year=252)


def test_buy_and_hold_does_not_pass() -> None:
    bars = daily(regimes())
    verdict = gate().evaluate(bars, [1.0] * len(bars), baseline_weights(bars, **PARAMS))
    assert not verdict.accepted and verdict.reasons


def test_the_baseline_does_not_pass_against_itself() -> None:
    bars = daily(regimes())
    base = baseline_weights(bars, **PARAMS)
    assert not gate().evaluate(bars, base, base).accepted  # equal is not better


def test_a_signal_that_really_adds_value_passes() -> None:
    bars = daily(regimes())
    # Perfect foresight: invested exactly before up days. Impossible in practice, so it shows
    # the gate can say yes.
    oracle = [1.0 if b.close > a.close else 0.0 for a, b in itertools.pairwise(bars)]
    verdict = gate().evaluate(bars, [*oracle, 0.0], baseline_weights(bars, **PARAMS))
    assert verdict.accepted, verdict.reasons


def test_cli_reports_the_verdict(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bars = daily(regimes())
    csv = write_csv(tmp_path / "spy.csv", bars)
    always = write_signal(tmp_path / "always.csv", bars, [1.0] * len(bars))
    args = ["research-gate", "--bars", str(csv), "--instrument", "SPY", "--trend", "100"]
    assert main([*args, "--signal", str(always)]) == 1
    assert "rejected:" in capsys.readouterr().out
    broken = write_signal(tmp_path / "broken.csv", bars[:10], [1.0] * 10)
    assert main([*args, "--signal", str(broken)]) == 2


def test_signal_weights_never_exceed_the_baseline_capital() -> None:
    bars = daily(regimes())
    assert all(0.0 <= w <= 1.0 for w in baseline_weights(bars, **PARAMS))
    assert Decimal(1) == weight_curve(bars, [0.0] * len(bars), cost_bps=0)[-1][1]
