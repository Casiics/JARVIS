"""The research lab: model workers propose, the research gate decides.

Two workers run in the supervisor-worker loop (`jarvis_core.orchestrator.loop`), each held to
a schema by Tier 0b and at most four attempts per sub-task:

- **Market researcher**: reads a brief of plain numbers about the instruments (returns,
  volatility, drawdown, trend, correlation) and proposes up to five hypotheses, each naming
  the features it relies on.
- **Model builder**: turns one hypothesis into a `SignalSpec` for one instrument. Its Tier 0b
  check runs the research gate on the research window; a rejection goes back to the builder
  as numbers ("Sharpe 0.41 vs baseline 0.63"), so the next attempt can improve on it.

Overfitting is the real risk of this design, since a builder that sees feedback will
eventually fit noise. Three rules keep that in check, all in plain code:

1. **Holdout.** The last 40 % of the bars are never shown to a worker: not in the brief, not
   in the feedback. A spec that passes on the research window is judged on the holdout once.
2. **Rising bar.** Every holdout test of an instrument raises the margin the next one must
   beat the baseline by (0.05 Sharpe per doubling of earlier tests), so trying many ideas
   does not make luck look like skill.
3. **Ledger.** Every spec is recorded with its fingerprint and verdicts
   (`<data-dir>/research/ledger.jsonl`); a rule already tested is refused before it costs a
   holdout test.

A spec that passes the holdout becomes a paper candidate in `<data-dir>/research/candidates/`
(spec plus a `ts,weight` signal file for `research-gate`). Nothing here places orders or
changes what trades on paper; that stays a human step.

This module never calls a model: the workers are handed in. The trading module stays free of
the model router (`tests/modules/test_domain_imports.py`), so model output can never reach the
order path. The command line that wires real workers lives in `trading_agents.lab`.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from jarvis_core.contracts.audit import AuditAction, AuditDecision, AuditLogEntry
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.task_token import TaskTokenClaims, issue_token, verify_token
from jarvis_core.orchestrator.loop import (
    TOOL_SUPERVISOR,
    TOOL_WORKER,
    Acceptance,
    Supervisor,
    SupervisorWorkerLoop,
    Worker,
    WorkOrder,
)
from mod_trading.feed import read_bars
from mod_trading.metrics import Performance
from mod_trading.research import ResearchGate, Verdict, baseline_weights
from mod_trading.schemas import Bar
from mod_trading.signals import FEATURES, Feature, SignalSpec, compile_spec, describe

ACTOR = "spiffe://jarvis.home/trading/research-lab"
RESEARCH_FRACTION = 0.6
HOLDOUT_STEP = 0.05

Stage = Literal["duplicate", "research_rejected", "holdout_rejected", "candidate"]


class Hypothesis(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=5, max_length=80)
    mechanism: str = Field(min_length=20, max_length=400)
    features: tuple[Feature, ...] = Field(min_length=1, max_length=4)
    instruments: tuple[str, ...] = Field(min_length=1, max_length=3)


class Hypotheses(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[Hypothesis, ...] = Field(min_length=1, max_length=5)


RESEARCHER_INSTRUCTION = (
    "You are a market researcher for a long-only ETF strategy that trades on paper. From the "
    "brief, propose up to five testable hypotheses for when holding an instrument pays and "
    "when it does not. Each must say which mechanism it relies on and which of these price "
    f"features could express it: {', '.join(FEATURES)}. Name only instruments from the brief. "
    "The bar to beat is a 200-bar trend filter with a 10 % volatility target, so propose ideas "
    "that differ from it, not variations of it."
)

BUILDER_INSTRUCTION = (
    "You are a model builder. Express the hypothesis as a signal specification for the "
    "instrument named in the inputs: up to four conditions on the price features, all of "
    "which must hold to be invested, and a sizing rule. Feature definitions: trend = close / "
    "moving average - 1; momentum = close / close `window` bars ago - 1; drawdown = close / "
    "highest close of the window - 1 (zero or negative); volatility = annualised standard "
    "deviation of log returns. Windows are in bars. Keep it simple: fewer conditions "
    "generalise better."
)


# --- deterministic side -------------------------------------------------------------------------


def _split(n: int) -> int:
    return max(1, int(n * RESEARCH_FRACTION))


def market_brief(series: Mapping[str, Sequence[Bar]], per_year: int) -> str:
    """Plain numbers about the research window of each instrument, as JSON."""
    out: dict[str, object] = {"bars_per_year": per_year, "instruments": {}}
    closes: dict[str, list[float]] = {}
    for name, bars in series.items():
        window = [float(b.close) for b in bars[: _split(len(bars))]]
        closes[name] = window
        returns = [b / a - 1 for a, b in itertools.pairwise(window)]
        mean = sum(returns) / len(returns)
        vol = math.sqrt(sum((r - mean) ** 2 for r in returns) / max(1, len(returns) - 1))
        peak, worst = window[0], 0.0
        for close in window:
            peak = max(peak, close)
            worst = min(worst, close / peak - 1)

        def back(k: int, w: list[float] = window) -> float | None:
            return round(w[-1] / w[-1 - k] - 1, 4) if len(w) > k else None

        out["instruments"][name] = {  # type: ignore[index]
            "bars": len(window),
            "from": bars[0].ts.date().isoformat(),
            "to": bars[len(window) - 1].ts.date().isoformat(),
            "total_return": round(window[-1] / window[0] - 1, 4),
            "annual_volatility": round(vol * math.sqrt(per_year), 4),
            "max_drawdown": round(worst, 4),
            "return_last_21_bars": back(21),
            "return_last_63_bars": back(63),
            "return_last_252_bars": back(252),
            "above_200_bar_average": (
                window[-1] > sum(window[-200:]) / 200 if len(window) >= 200 else None
            ),
        }
    pairs: dict[str, float] = {}
    for left, right in itertools.combinations(closes, 2):
        n = min(len(closes[left]), len(closes[right]))
        a = [y / x - 1 for x, y in itertools.pairwise(closes[left][:n])]
        b = [y / x - 1 for x, y in itertools.pairwise(closes[right][:n])]
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        cov = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
        den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
        pairs[f"{left}/{right}"] = round(cov / den, 3) if den else 0.0
    out["return_correlation"] = pairs
    return json.dumps(out, sort_keys=True)


class LedgerEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    at: datetime
    session: str
    instrument: str
    fingerprint: str
    spec: SignalSpec | None
    stage: Stage
    attempts: int
    research_sharpe: float | None = None
    baseline_research_sharpe: float | None = None
    holdout_sharpe: float | None = None
    baseline_holdout_sharpe: float | None = None
    holdout_margin: float | None = None
    reasons: tuple[str, ...] = ()


@dataclass
class Ledger:
    path: Path

    def entries(self) -> list[LedgerEntry]:
        if not self.path.exists():
            return []
        lines = self.path.read_text("utf-8").splitlines()
        return [LedgerEntry.model_validate_json(line) for line in lines if line.strip()]

    def tested(self, instrument: str) -> set[str]:
        return {e.fingerprint for e in self.entries() if e.instrument == instrument and e.spec}

    def holdout_tests(self, instrument: str) -> int:
        return sum(
            1
            for e in self.entries()
            if e.instrument == instrument and e.stage in ("holdout_rejected", "candidate")
        )

    def append(self, entry: LedgerEntry) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        line = entry.model_dump_json()
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return hashlib.sha256(line.encode()).hexdigest()


@dataclass
class Workbench:
    """Bars, baseline and gate for one session. Splits every series into research and holdout."""

    series: Mapping[str, Sequence[Bar]]
    per_year: int
    gate: ResearchGate
    baseline_params: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._baseline = {
            name: baseline_weights(bars, **{"bars_per_year": self.per_year, **self.baseline_params})
            for name, bars in self.series.items()
        }

    def evaluate(self, instrument: str, spec: SignalSpec, window: str) -> Verdict:
        """Weights are computed on the whole series (causal), then judged on one window."""
        bars = self.series[instrument]
        weights = compile_spec(spec, bars, self.per_year)
        cut = _split(len(bars))
        part = slice(0, cut) if window == "research" else slice(cut, len(bars))
        return self.gate.evaluate(bars[part], weights[part], self._baseline[instrument][part])


def _sharpe(p: Performance) -> float | None:
    return None if p.sharpe is None else round(p.sharpe, 3)


def _feedback(verdict: Verdict) -> str:
    c, b = verdict.candidate, verdict.baseline
    co, bo = verdict.candidate_oos, verdict.baseline_oos
    return (
        f"Rejected on the research window: Sharpe {c.sharpe or 0:.2f} vs baseline "
        f"{b.sharpe or 0:.2f} (whole), {co.sharpe or 0:.2f} vs {bo.sharpe or 0:.2f} (second "
        f"half); max drawdown {c.max_drawdown:.1%} vs {b.max_drawdown:.1%}. "
        + "; ".join(verdict.reasons)
    )


# --- the session --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    instrument: str
    hypothesis: str
    stage: Stage
    spec: SignalSpec | None
    detail: str


@dataclass
class ResearchLab:
    bench: Workbench
    ledger: Ledger
    candidates: Path
    loop: SupervisorWorkerLoop
    researcher: Worker
    builder: Worker
    claims: Callable[[], TaskTokenClaims]
    audit: Callable[[AuditLogEntry], object]
    supervisor: Supervisor | None = None
    session: str = field(default_factory=lambda: uuid4().hex)

    def run(self) -> list[Outcome]:
        known = tuple(self.bench.series)
        hypotheses = self._hypotheses(known)
        outcomes: list[Outcome] = []
        for hypothesis in hypotheses:
            for instrument in hypothesis.instruments:
                outcomes.append(self._build(hypothesis, instrument))
        return outcomes

    def _order(self, instruction: str, inputs: tuple[tuple[str, str], ...]) -> WorkOrder:
        return WorkOrder(
            claims=self.claims(),
            trace_id=self.session,
            instruction=instruction,
            # Market prices and figures derived from them are public; the label lets the
            # router pick any model class it allows for trading.
            label=DataClassification.PUBLIC,
            inputs=inputs,
        )

    def _hypotheses(self, known: tuple[str, ...]) -> tuple[Hypothesis, ...]:
        def only_known(value: Hypotheses) -> str | None:
            unknown = {i for h in value.items for i in h.instruments} - set(known)
            return f"unknown instruments {sorted(unknown)}; use {list(known)}" if unknown else None

        outcome = self.loop.run(
            self._order(
                RESEARCHER_INSTRUCTION,
                (("brief", market_brief(self.bench.series, self.bench.per_year)),),
            ),
            Acceptance(Hypotheses, checks=(only_known,)),
            self.researcher,
            self.supervisor,
        )
        return outcome.value.items if outcome.value else ()

    def _build(self, hypothesis: Hypothesis, instrument: str) -> Outcome:
        tested = self.ledger.tested(instrument)
        verdicts: dict[str, Verdict] = {}

        def new_rule(spec: SignalSpec) -> str | None:
            if spec.fingerprint() in tested:
                return "this exact rule was already tested on this instrument; propose another"
            return None

        def passes_research(spec: SignalSpec) -> str | None:
            verdict = self.bench.evaluate(instrument, spec, "research")
            verdicts[spec.fingerprint()] = verdict
            return None if verdict.accepted else _feedback(verdict)

        brief = json.dumps(
            {"instrument": instrument, "hypothesis": hypothesis.model_dump(mode="json")},
            sort_keys=True,
        )
        outcome = self.loop.run(
            self._order(BUILDER_INSTRUCTION, (("task", brief),)),
            Acceptance(SignalSpec, checks=(new_rule, passes_research)),
            self.builder,
            self.supervisor,
        )
        spec = outcome.value
        if spec is None:
            last = list(verdicts.values())[-1] if verdicts else None
            self._record(
                instrument,
                None,
                "research_rejected",
                outcome.attempts,
                research=last,
                reasons=last.reasons if last else (outcome.error_class or "no valid spec",),
            )
            return Outcome(
                instrument, hypothesis.title, "research_rejected", None, "no spec passed"
            )
        prior = self.ledger.holdout_tests(instrument)
        margin = self.bench.gate.margin + HOLDOUT_STEP * math.log2(1 + prior)
        gate = ResearchGate(
            bars_per_year=self.bench.gate.bars_per_year,
            cost_bps=self.bench.gate.cost_bps,
            margin=margin,
            max_extra_drawdown=self.bench.gate.max_extra_drawdown,
        )
        holdout = Workbench(self.bench.series, self.bench.per_year, gate,
                            self.bench.baseline_params)  # fmt: skip
        verdict = holdout.evaluate(instrument, spec, "holdout")
        stage: Stage = "candidate" if verdict.accepted else "holdout_rejected"
        self._record(
            instrument,
            spec,
            stage,
            outcome.attempts,
            research=verdicts.get(spec.fingerprint()),
            holdout=verdict,
            margin=margin,
            reasons=verdict.reasons,
        )
        if verdict.accepted:
            self._write_candidate(instrument, spec)
        return Outcome(instrument, hypothesis.title, stage, spec, describe(spec))

    def _record(
        self,
        instrument: str,
        spec: SignalSpec | None,
        stage: Stage,
        attempts: int,
        *,
        research: Verdict | None = None,
        holdout: Verdict | None = None,
        margin: float | None = None,
        reasons: tuple[str, ...] = (),
    ) -> None:
        entry = LedgerEntry(
            at=datetime.now(UTC),
            session=self.session,
            instrument=instrument,
            fingerprint=spec.fingerprint() if spec else "-",
            spec=spec,
            stage=stage,
            attempts=attempts,
            research_sharpe=_sharpe(research.candidate) if research else None,
            baseline_research_sharpe=_sharpe(research.baseline) if research else None,
            holdout_sharpe=_sharpe(holdout.candidate) if holdout else None,
            baseline_holdout_sharpe=_sharpe(holdout.baseline) if holdout else None,
            holdout_margin=round(margin, 3) if margin is not None else None,
            reasons=reasons,
        )
        digest = self.ledger.append(entry)
        self.audit(
            AuditLogEntry(
                trace_id=self.session,
                domain=Domain.TRADING,
                actor=ACTOR,
                action=AuditAction.STATE_TRANSITION,
                target=f"research:{instrument}:{entry.fingerprint}:{stage}",
                decision=AuditDecision.ALLOW if stage == "candidate" else AuditDecision.DENY,
                content_hash=digest,
                content_ref=f"research/ledger.jsonl#{self.session}",
            )
        )

    def _write_candidate(self, instrument: str, spec: SignalSpec) -> None:
        bars = self.bench.series[instrument]
        weights = compile_spec(spec, bars, self.bench.per_year)
        self.candidates.mkdir(parents=True, exist_ok=True, mode=0o700)
        stem = self.candidates / f"{instrument}-{spec.fingerprint()}"
        stem.with_suffix(".json").write_text(spec.model_dump_json(indent=2), "utf-8")
        rows = [
            "ts,weight",
            *(f"{b.ts.isoformat()},{w:.6f}" for b, w in zip(bars, weights, strict=True)),
        ]
        stem.with_suffix(".csv").write_text("\n".join(rows) + "\n", "utf-8")


# --- helpers for the command line --------------------------------------------------------------


def research_claims() -> TaskTokenClaims:
    key = secrets.token_bytes(32)
    return verify_token(
        key,
        issue_token(
            key,
            task_id=uuid4(),
            domain=Domain.TRADING,
            allowed_tools=frozenset({TOOL_WORKER, TOOL_SUPERVISOR}),
            budget_eur=0.0,
            ttl=timedelta(minutes=30),
        ),
    )


def load_series(paths: Sequence[Path]) -> dict[str, list[Bar]]:
    series: dict[str, list[Bar]] = {}
    for path in paths:
        for bar in read_bars(path):
            series.setdefault(bar.instrument, []).append(bar)
    for bars in series.values():
        bars.sort(key=lambda b: b.ts)
    return series
