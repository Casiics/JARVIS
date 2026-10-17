"""Research workers propose signal specs; plain code compiles, judges and records them.

Workers are scripted here, so the tests pin down the deterministic side: specs cannot look
ahead, the holdout never reaches a worker, a rejected spec goes back as numbers, a rule is
tested once, and every further holdout test raises the bar.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from jarvis_core.agents.runtime import AgentReply
from jarvis_core.audit.chain import AuditWriter, InMemoryAuditStore
from jarvis_core.contracts.audit import AuditLogEntry
from jarvis_core.orchestrator.loop import SupervisorWorkerLoop, WorkOrder
from jarvis_core.orchestrator.state_machine import TaskMachine
from mod_trading.lab import (
    HOLDOUT_STEP,
    Ledger,
    ResearchLab,
    Workbench,
    _claims,
    _split,
    market_brief,
)
from mod_trading.research import ResearchGate
from mod_trading.signals import SignalSpec, compile_spec, describe, feature_value
from tests.trading.test_trend_vol import daily, regimes

TREND: dict[str, Any] = {
    "name": "trend-50",
    "rationale": "Hold while price is above its 50-bar average.",
    "conditions": [{"feature": "trend", "window": 50, "op": "above", "threshold": 0.0}],
    "sizing": "full",
}
MOMENTUM: dict[str, Any] = {
    "name": "momentum-60",
    "rationale": "Hold after a positive quarter, sized to a volatility target.",
    "conditions": [{"feature": "momentum", "window": 60, "op": "above", "threshold": 0.0}],
    "sizing": "vol_target",
    "target_vol": 0.12,
}
HYPOTHESES: dict[str, Any] = {
    "items": [
        {
            "title": "Momentum persists",
            "mechanism": "Funds chase recent winners, so a positive quarter tends to continue.",
            "features": ["momentum"],
            "instruments": ["SPY"],
        }
    ]
}


def spec(data: dict[str, Any]) -> SignalSpec:
    return SignalSpec.model_validate(data)


# --- signal specs -------------------------------------------------------------------------------


def test_weights_never_look_ahead() -> None:
    bars = daily(regimes())
    weights = compile_spec(spec(MOMENTUM), bars, 252)
    changed = [*bars[:500], *daily([c * 3 for c in regimes()])[500:]]
    assert compile_spec(spec(MOMENTUM), changed, 252)[:500] == weights[:500]
    assert all(w == 0.0 for w in weights[:60])  # warming up: 60 returns and today
    assert all(0.0 <= w <= 1.0 for w in weights)


def test_features_are_what_their_names_say() -> None:
    closes = [100.0, 110.0, 99.0, 121.0]
    assert feature_value("trend", closes, 2, 252) == pytest.approx(121 / 110 - 1)
    assert feature_value("momentum", closes, 3, 252) == pytest.approx(0.21)
    assert feature_value("drawdown", closes[:3], 2, 252) == pytest.approx(99 / 110 - 1)
    flat = feature_value("volatility", [100.0] * 5, 4, 252)
    assert flat == 0.0


def test_fingerprint_is_the_rule_not_its_name() -> None:
    renamed = spec({**TREND, "name": "other-name", "rationale": "Same rule, other words."})
    assert renamed.fingerprint() == spec(TREND).fingerprint()
    assert spec(MOMENTUM).fingerprint() != spec(TREND).fingerprint()
    assert describe(spec(TREND)) == "trend(50) above 0 -> full"


@pytest.mark.parametrize(
    "bad",
    [
        {**TREND, "conditions": []},
        {**TREND, "conditions": [{**TREND["conditions"][0], "feature": "sentiment"}]},
        {**TREND, "sizing": "leveraged"},
        {**TREND, "name": "Has Spaces"},
        {**TREND, "extra": "field"},
    ],
)
def test_specs_outside_the_library_are_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec(bad)


# --- the lab ------------------------------------------------------------------------------------


class Scripted:
    """Answers each attempt with the next scripted reply and keeps the feedback it got."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = replies
        self.orders: list[WorkOrder] = []
        self.feedback: list[tuple[str, ...]] = []

    def attempt(
        self, order: WorkOrder, schema: dict[str, Any], feedback: tuple[str, ...]
    ) -> AgentReply:
        self.orders.append(order)
        self.feedback.append(feedback)
        return AgentReply(text=json.dumps(self.replies.pop(0)))


def make_lab(
    tmp_path: Path, builder: Scripted, *, margin: float, researcher: Scripted | None = None
) -> tuple[ResearchLab, list[AuditLogEntry]]:
    series = {"SPY": daily(regimes())}
    audited: list[AuditLogEntry] = []
    writer = AuditWriter(InMemoryAuditStore())

    def audit(entry: AuditLogEntry) -> object:
        audited.append(entry)
        return writer.write(entry)

    lab = ResearchLab(
        bench=Workbench(
            series,
            252,
            ResearchGate(
                bars_per_year=252, margin=margin, max_extra_drawdown=1.0 if margin < 0 else 0.05
            ),
            {"trend": 100},
        ),
        ledger=Ledger(tmp_path / "ledger.jsonl"),
        candidates=tmp_path / "candidates",
        loop=SupervisorWorkerLoop(TaskMachine(writer.write, "spiffe://test/lab")),
        researcher=researcher or Scripted([HYPOTHESES]),
        builder=builder,
        claims=_claims,
        audit=audit,
    )
    return lab, audited


def test_the_brief_covers_the_research_window_only() -> None:
    bars = daily(regimes())
    brief = json.loads(market_brief({"SPY": bars, "QQQ": daily(regimes(3), "QQQ")}, 252))
    spy = brief["instruments"]["SPY"]
    assert spy["bars"] == _split(len(bars))
    assert spy["to"] == bars[_split(len(bars)) - 1].ts.date().isoformat()
    assert set(brief["return_correlation"]) == {"SPY/QQQ"}


def test_a_spec_that_clears_the_gate_becomes_a_paper_candidate(tmp_path: Path) -> None:
    builder = Scripted([MOMENTUM])
    lab, audited = make_lab(tmp_path, builder, margin=-10.0)  # any spec clears this gate
    [outcome] = lab.run()
    assert outcome.stage == "candidate"
    fingerprint = spec(MOMENTUM).fingerprint()
    signal = tmp_path / "candidates" / f"SPY-{fingerprint}.csv"
    assert signal.read_text().startswith("ts,weight\n")
    [entry] = lab.ledger.entries()
    assert entry.stage == "candidate" and entry.attempts == 1
    assert entry.holdout_margin == pytest.approx(-10.0)
    research = [a for a in audited if a.target.startswith("research:")]
    assert research[0].target == f"research:SPY:{fingerprint}:candidate"
    assert research[0].content_hash is not None  # the audit holds a hash, not the spec


def test_a_rejected_spec_goes_back_as_numbers(tmp_path: Path) -> None:
    builder = Scripted([TREND, MOMENTUM, TREND, MOMENTUM])
    lab, _ = make_lab(tmp_path, builder, margin=10.0)  # nothing clears this gate
    [outcome] = lab.run()
    assert outcome.stage == "research_rejected"
    assert len(builder.feedback) == 4  # the loop's iteration limit
    second = " ".join(builder.feedback[1])
    assert "Sharpe" in second and "vs baseline" in second and "drawdown" in second
    [entry] = lab.ledger.entries()
    assert entry.spec is None and entry.attempts == 4


def test_the_holdout_never_reaches_a_worker(tmp_path: Path) -> None:
    builder = Scripted([TREND, MOMENTUM, TREND, MOMENTUM])
    researcher = Scripted([HYPOTHESES])
    lab, _ = make_lab(tmp_path, builder, margin=10.0, researcher=researcher)
    lab.run()
    bars = lab.bench.series["SPY"]
    holdout_days = {b.ts.date().isoformat() for b in bars[_split(len(bars)) :]}
    seen = " ".join(
        f"{v} {' '.join(f)}"
        for w in (builder, researcher)
        for o, f in zip(w.orders, w.feedback, strict=True)
        for _k, v in o.inputs
    )
    assert not any(day in seen for day in holdout_days)


def test_a_rule_is_tested_once(tmp_path: Path) -> None:
    lab, _ = make_lab(tmp_path, Scripted([MOMENTUM]), margin=-10.0)
    lab.run()
    renamed = {**MOMENTUM, "name": "same-rule"}
    builder = Scripted([renamed, TREND])
    lab2, _ = make_lab(tmp_path, builder, margin=-10.0)
    [outcome] = lab2.run()
    assert "already tested" in " ".join(builder.feedback[1])
    assert outcome.spec is not None and outcome.spec.name == "trend-50"


def test_every_holdout_test_raises_the_bar(tmp_path: Path) -> None:
    for _ in range(3):
        rules = [
            {**TREND, "conditions": [{**TREND["conditions"][0], "window": w}]}
            for w in (20 + _ * 10,)
        ]
        lab, _a = make_lab(tmp_path, Scripted(rules), margin=-10.0)
        lab.run()
    margins = [e.holdout_margin for e in Ledger(tmp_path / "ledger.jsonl").entries()]
    expected = [-10.0 + HOLDOUT_STEP * math.log2(1 + n) for n in range(3)]
    assert margins == pytest.approx(expected, abs=1e-3)


def test_unknown_instruments_go_back_to_the_researcher(tmp_path: Path) -> None:
    wrong = {"items": [{**HYPOTHESES["items"][0], "instruments": ["TSLA"]}]}
    researcher = Scripted([wrong, HYPOTHESES])
    lab, _ = make_lab(tmp_path, Scripted([MOMENTUM]), margin=-10.0, researcher=researcher)
    [outcome] = lab.run()
    assert "unknown instruments" in " ".join(researcher.feedback[1])
    assert outcome.instrument == "SPY"


def test_the_ledger_command_lists_what_was_tested(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from mod_trading.lab import main

    lab, _ = make_lab(tmp_path / "research", Scripted([MOMENTUM]), margin=-10.0)
    lab.run()
    assert main(["--data-dir", str(tmp_path), "ledger"]) == 0
    out = capsys.readouterr().out
    assert "SPY" in out and "candidate" in out and "momentum(60) above 0" in out
