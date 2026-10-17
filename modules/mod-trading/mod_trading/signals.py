"""Signal specifications: how a research agent proposes a trading idea without writing code.

A model never writes code that runs here. It fills in a `SignalSpec`: up to four conditions on
a small library of causal price features, combined with AND, plus a sizing rule. Plain code
turns the spec into one weight per bar (`compile_spec`), and the research gate judges the
weights. The library is deliberately small: every feature uses only closes up to and
including the bar it is computed on, so a spec cannot look ahead, and a spec is short enough
for a human to read in the ledger.

Features (window in bars):
- `trend`: close over its simple moving average, minus 1.
- `momentum`: close over the close `window` bars earlier, minus 1.
- `drawdown`: close over the highest close of the window, minus 1 (0 at a new high, negative
  below it).
- `volatility`: annualised standard deviation of the window's log returns.
"""

from __future__ import annotations

import hashlib
import itertools
import math
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from mod_trading.schemas import Bar

Feature = Literal["trend", "momentum", "drawdown", "volatility"]
FEATURES: tuple[Feature, ...] = ("trend", "momentum", "drawdown", "volatility")


class SpecError(Exception):
    pass


class Condition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    feature: Feature
    window: int = Field(ge=2, le=300)
    op: Literal["above", "below"]
    threshold: float = Field(ge=-1.0, le=3.0)


class SignalSpec(BaseModel):
    """Invested while every condition holds; sized by `sizing`; long only, no leverage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=3, max_length=60, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    rationale: str = Field(min_length=10, max_length=400)
    conditions: tuple[Condition, ...] = Field(min_length=1, max_length=4)
    sizing: Literal["full", "vol_target"]
    target_vol: float = Field(default=0.10, ge=0.03, le=0.30)
    vol_window: int = Field(default=20, ge=5, le=120)

    def fingerprint(self) -> str:
        """Identifies the trading rule; the name and rationale do not change it."""
        rule = self.model_dump_json(include={"conditions", "sizing", "target_vol", "vol_window"})
        return hashlib.sha256(rule.encode()).hexdigest()[:16]

    def warmup(self) -> int:
        windows = [c.window for c in self.conditions]
        if self.sizing == "vol_target":
            windows.append(self.vol_window)
        return max(windows) + 1


def feature_value(feature: Feature, closes: Sequence[float], window: int, per_year: int) -> float:
    """The feature on the last close of `closes`, which must hold at least window + 1 closes."""
    if len(closes) < window + 1:
        raise SpecError("not enough history")
    last = closes[-1]
    if feature == "trend":
        mean = sum(closes[-window:]) / window
        return last / mean - 1
    if feature == "momentum":
        return last / closes[-1 - window] - 1
    if feature == "drawdown":
        return last / max(closes[-window:]) - 1
    returns = [math.log(b / a) for a, b in itertools.pairwise(closes[-window - 1 :])]
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / max(1, len(returns) - 1)
    return math.sqrt(variance * per_year)


def compile_spec(spec: SignalSpec, bars: Sequence[Bar], per_year: int) -> list[float]:
    """One weight in [0, 1] per bar, from closes up to that bar only; 0 while warming up."""
    closes = [float(b.close) for b in bars]
    if any(c <= 0 for c in closes):
        raise SpecError("non-positive close")
    need = spec.warmup()
    weights: list[float] = []
    for i in range(len(closes)):
        history = closes[: i + 1]
        if len(history) < need:
            weights.append(0.0)
            continue
        invested = all(
            (feature_value(c.feature, history, c.window, per_year) > c.threshold)
            == (c.op == "above")
            for c in spec.conditions
        )
        if not invested:
            weights.append(0.0)
        elif spec.sizing == "full":
            weights.append(1.0)
        else:
            vol = feature_value("volatility", history, spec.vol_window, per_year)
            weights.append(1.0 if vol == 0 else min(1.0, spec.target_vol / vol))
    return weights


def describe(spec: SignalSpec) -> str:
    """One line a human can check against the weights."""
    parts = [f"{c.feature}({c.window}) {c.op} {c.threshold:g}" for c in spec.conditions]
    size = "full" if spec.sizing == "full" else f"vol target {spec.target_vol:g}/{spec.vol_window}"
    return f"{' and '.join(parts)} -> {size}"
