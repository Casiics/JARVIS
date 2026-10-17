"""Inbox ideas assessed by the supervisor-worker loop: notes for the owner, never orders."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from jarvis_core.agents.hermes import HermesRuntime
from jarvis_core.agents.runtime import GatewayRuntime
from jarvis_core.agents.select import RuntimeUnavailable, local_runtime
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import Taint, TradingIdeaProposed
from jarvis_core.orchestrator.loop import RuntimeSupervisor, RuntimeWorker, SupervisorWorkerLoop
from jarvis_core.orchestrator.state_machine import TaskMachine, TaskState
from jarvis_core.router.budget import CircuitBreaker, InMemorySpendLedger
from jarvis_core.router.gateway import InferenceGateway
from mod_trading.inbox import TradingInbox
from tests.orchestrator.fakes import RecordingModel, ScriptedRuntime, claims
from trading_agents.review import AssessmentStore, IdeaReviewer, Verdict, review_open_ideas

GOOD = (
    '{"verdict": "research", "instruments": ["SPY"], '
    '"rationale": "A momentum claim worth one backtest against the baseline."}'
)
OK = '{"accept": true}'


def inbox_with_idea(tmp_path: Path, instruments: tuple[str, ...] = ("SPY",)) -> TradingInbox:
    inbox = TradingInbox(tmp_path / "ideas.json")
    inbox.receive(
        TradingIdeaProposed(
            origin_domain=Domain.CONTENT,
            target_domain=Domain.TRADING,
            classification=DataClassification.PUBLIC,
            taint=Taint.EXTERNAL_CONTENT,
            summary="Buy SPY when it closes above its 200-day average.",
            instruments=instruments,
            source_ref="reel:abc",
        ),
        sender=Domain.CONTENT,
    )
    return inbox


def reviewer(runtime: ScriptedRuntime) -> IdeaReviewer:
    return IdeaReviewer(
        loop=SupervisorWorkerLoop(TaskMachine(lambda _e: None, actor="test")),
        worker=RuntimeWorker(runtime),
        supervisor=RuntimeSupervisor(runtime),
        claims=lambda: claims(Domain.TRADING, taint=Taint.EXTERNAL_CONTENT, budget_eur=0.0),
        clock=lambda: datetime(2026, 10, 17, tzinfo=UTC),
    )


def test_an_idea_gets_a_reviewed_assessment(tmp_path: Path) -> None:
    inbox = inbox_with_idea(tmp_path)
    store = AssessmentStore(tmp_path / "assessments.json")
    runtime = ScriptedRuntime(Domain.TRADING, worker=[GOOD], supervisor=[OK])

    [result] = review_open_ideas(inbox, store, reviewer(runtime))

    assert result.state is TaskState.DONE and result.assessment is not None
    assert result.assessment.verdict is Verdict.RESEARCH
    assert runtime.roles() == ["worker", "supervisor"]
    # The idea is an input, the token stays tainted, and nothing touched the inbox.
    _, scope = runtime.turns[0]
    assert scope.taint is Taint.EXTERNAL_CONTENT
    assert "200-day average" in runtime.turns[0][0].prompt
    assert not inbox.all()[0].dismissed
    # Reviewed ideas are skipped next time.
    assert review_open_ideas(inbox, store, reviewer(runtime)) == []


def test_tier_0b_refuses_instruments_the_idea_never_named(tmp_path: Path) -> None:
    inbox = inbox_with_idea(tmp_path)
    store = AssessmentStore(tmp_path / "assessments.json")
    invented = GOOD.replace('["SPY"]', '["TSLA"]')
    runtime = ScriptedRuntime(Domain.TRADING, worker=[invented, GOOD], supervisor=[OK])

    [result] = review_open_ideas(inbox, store, reviewer(runtime))

    assert result.state is TaskState.DONE and result.attempts == 2
    assert "TSLA" in runtime.turns[1][0].prompt  # the gate's error went back to the worker


def test_a_failed_review_is_stored_and_retried_later(tmp_path: Path) -> None:
    inbox = inbox_with_idea(tmp_path)
    store = AssessmentStore(tmp_path / "assessments.json")
    down = ScriptedRuntime(Domain.TRADING, fail_with="model_unavailable")

    [failed] = review_open_ideas(inbox, store, reviewer(down))
    assert failed.state is TaskState.ESCALATED and failed.error_class == "model_unavailable"

    again = ScriptedRuntime(Domain.TRADING, worker=[GOOD], supervisor=[OK])
    [retried] = review_open_ideas(inbox, store, reviewer(again))
    assert retried.state is TaskState.DONE


def _gateway() -> InferenceGateway:
    return InferenceGateway(
        local=RecordingModel(),
        breaker=CircuitBreaker(InMemorySpendLedger()),
        audit=lambda _e: None,
        actor="test",
    )


def test_hermes_is_the_default_runtime_when_installed() -> None:
    found = local_runtime(_gateway(), Domain.TRADING, which=lambda _n: "/usr/bin/hermes")
    assert isinstance(found, HermesRuntime) and found.require_local
    missing = local_runtime(_gateway(), Domain.TRADING, which=lambda _n: None)
    assert isinstance(missing, GatewayRuntime) and missing.require_local
    with pytest.raises(RuntimeUnavailable):
        local_runtime(_gateway(), Domain.TRADING, choice="hermes", which=lambda _n: None)
