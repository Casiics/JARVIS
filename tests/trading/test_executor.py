"""The executor: gate, then risk, then broker, with the kill switch beside every step."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from jarvis_core.contracts.audit import AuditAction, AuditDecision
from jarvis_core.contracts.events import Taint
from mod_trading.executor import LiveTradingDisabled, OrderExecutor, Outcome
from mod_trading.schemas import OrderIntent, OrderStatus, OrderType, Side
from tests.trading.helpers import Rig, claims
from trading_risk import ViolationCode


def intent(side: Side = Side.BUY, qty: str = "1", **kwargs: object) -> OrderIntent:
    return OrderIntent(
        strategy="test",
        instrument=str(kwargs.pop("instrument", "SPY")),
        side=side,
        quantity=Decimal(qty),
        reason="unit test",
        **kwargs,
    )


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    r = Rig(tmp_path)
    r.price("SPY", "500")
    return r


def test_order_passes_gate_risk_and_fills(rig: Rig) -> None:
    result = rig.executor.submit(intent(), claims())
    assert result.outcome is Outcome.FILLED
    assert rig.broker.get_positions() == {"SPY": Decimal(1)}
    actions = [(e.action, e.decision) for e in rig.entries]
    assert actions == [
        (AuditAction.GATE_DECISION, AuditDecision.ALLOW),
        (AuditAction.GATE_DECISION, AuditDecision.ALLOW),
        (AuditAction.TOOL_CALL, AuditDecision.ALLOW),
    ]


def test_audit_holds_references_not_order_content(rig: Rig) -> None:
    rig.executor.submit(intent(), claims())
    dumped = json.dumps([e.model_dump(mode="json") for e in rig.entries])
    assert "unit test" not in dumped
    assert all(e.content_hash for e in rig.entries)


def test_token_without_the_tool_is_rejected_by_the_gate(rig: Rig) -> None:
    result = rig.executor.submit(intent(), claims(tools=frozenset()))
    assert result.outcome is Outcome.REJECTED_GATE
    assert rig.broker.state.orders == {}


def test_tainted_task_cannot_trade_without_approval(rig: Rig) -> None:
    result = rig.executor.submit(intent(), claims(taint=Taint.EXTERNAL_CONTENT))
    assert result.outcome is Outcome.REJECTED_GATE
    assert "approval" in result.detail
    assert rig.broker.state.orders == {}


def test_risk_violation_never_reaches_the_broker(rig: Rig) -> None:
    result = rig.executor.submit(intent(qty="5"), claims())
    assert result.outcome is Outcome.REJECTED_RISK
    assert {v.code for v in result.violations} >= {ViolationCode.ORDER_NOTIONAL}
    assert rig.broker.state.orders == {}
    assert rig.entries[-1].policy_id == "trading_risk"
    assert rig.entries[-1].decision is AuditDecision.DENY


def test_engaged_kill_switch_blocks_orders(rig: Rig) -> None:
    rig.kill_switch.engage("manual")
    result = rig.executor.submit(intent(), claims())
    assert result.outcome is Outcome.REJECTED_RISK
    assert {v.code for v in result.violations} == {ViolationCode.KILL_SWITCH}
    assert rig.broker.state.orders == {}


def test_missing_kill_switch_file_blocks_orders(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.price("SPY", "500")
    (tmp_path / "killswitch").unlink()
    assert rig.executor.submit(intent(), claims()).outcome is Outcome.REJECTED_RISK


def test_daily_loss_trips_the_switch_and_cancels_open_orders(rig: Rig) -> None:
    rig.price("SPY", "400")
    assert rig.executor.submit(intent(qty="2"), claims()).outcome is Outcome.FILLED
    resting = rig.executor.submit(
        intent(qty="1", order_type=OrderType.LIMIT, limit_price=Decimal("395")), claims()
    )
    assert resting.outcome is Outcome.OPEN
    rig.price("SPY", "290")  # -220 on two shares: beyond the 200 daily loss limit
    breaches = rig.executor.mark_to_market()
    assert ViolationCode.DAILY_LOSS in {v.code for v in breaches}
    assert rig.kill_switch.engaged()
    assert "daily_loss" in rig.kill_switch.state().reason
    assert rig.broker.open_orders() == []
    assert any(e.target.startswith("kill_switch:engaged") for e in rig.entries)
    # Even selling to cut the loss now needs a human to release the switch.
    assert rig.executor.submit(intent(Side.SELL, "2"), claims()).outcome is Outcome.REJECTED_RISK


def test_order_frequency_is_counted_by_the_executor(tmp_path: Path) -> None:
    rig = Rig(tmp_path, max_orders_per_minute=2)
    rig.price("SPY", "100")
    outcomes = [rig.executor.submit(intent(), claims()).outcome for _ in range(3)]
    assert outcomes == [Outcome.FILLED, Outcome.FILLED, Outcome.REJECTED_RISK]


def test_broker_rejection_is_reported(rig: Rig) -> None:
    result = rig.executor.submit(intent(Side.SELL), claims())
    # Shorting is stopped by risk before the broker would refuse it.
    assert result.outcome is Outcome.REJECTED_RISK
    rig.executor.engine = type(rig.executor.engine)(
        rig.executor.engine.limits.model_copy(update={"allow_short": True})
    )
    result = rig.executor.submit(intent(Side.SELL), claims())
    assert result.outcome is Outcome.REJECTED_BROKER
    assert result.order is not None and result.order.status is OrderStatus.REJECTED


def test_only_paper_brokers_are_accepted(rig: Rig) -> None:
    class LiveBroker(type(rig.broker)):  # type: ignore[misc]
        @property
        def mode(self) -> str:
            return "live"

    with pytest.raises(LiveTradingDisabled):
        OrderExecutor(
            broker=LiveBroker(),
            engine=rig.executor.engine,
            kill_switch=rig.kill_switch,
            tracker=rig.executor.tracker,
            audit=rig.executor.audit,
        )
