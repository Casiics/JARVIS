"""The order executor: the only code that hands orders to a broker.

For every order intent, in this order:
1. The action gate decides the ActionRequest against the task token (`trading.place_order`).
2. The broker's quote prices the order; the risk engine checks it against the limits, the
   account and the kill switch. A daily-loss or drawdown breach engages the kill switch.
3. Only then does the broker see the order.

Each step is audited with references only. No model is involved anywhere in this module, and
it refuses any broker that is not a paper broker until live trading is decided by a human.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from jarvis_core.contracts.actions import ActionRequest, ApprovalTier
from jarvis_core.contracts.audit import AuditAction, AuditDecision, AuditLogEntry
from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.task_token import TaskTokenClaims
from jarvis_core.gate.action_gate import GateOutcome, decide
from mod_trading.account import EquityTracker
from mod_trading.broker import BrokerError, PaperBroker
from mod_trading.schemas import Order, OrderIntent, OrderRequest, OrderStatus, OrderType, Side
from trading_risk import AccountView, KillSwitch, OrderCheck, RiskEngine, Violation

TOOL_PLACE_ORDER = "trading.place_order"
TRADING_TOOLS = frozenset({TOOL_PLACE_ORDER})
ACTOR = "spiffe://jarvis.home/trading/executor"


class LiveTradingDisabled(Exception):
    """Raised when anything but the paper broker is wired in (Phase 6 is paper only)."""


class Outcome(StrEnum):
    FILLED = "filled"
    OPEN = "open"
    REJECTED_GATE = "rejected_gate"
    REJECTED_RISK = "rejected_risk"
    REJECTED_BROKER = "rejected_broker"


@dataclass(frozen=True)
class ExecutionResult:
    intent: OrderIntent
    outcome: Outcome
    order: Order | None = None
    violations: tuple[Violation, ...] = ()
    detail: str = ""


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class OrderExecutor:
    broker: PaperBroker
    engine: RiskEngine
    kill_switch: KillSwitch
    tracker: EquityTracker
    audit: Callable[[AuditLogEntry], object]
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.broker.mode != "paper":
            raise LiveTradingDisabled("Phase 6 trades on paper only")

    def account_view(self, now: datetime) -> AccountView:
        positions = self.broker.get_positions()
        marks = {i: self.broker.get_quote(i).mid for i in positions if i in self.broker.quotes}
        equity = self.broker.equity(marks)
        self.tracker.observe(equity, now)
        state = self.tracker.state
        assert state.day_start_equity is not None and state.peak_equity is not None
        return AccountView(
            equity=equity,
            day_start_equity=state.day_start_equity,
            peak_equity=state.peak_equity,
            positions=positions,
            marks=marks,
            orders_sent=tuple(state.orders_sent),
        )

    def mark_to_market(self) -> tuple[Violation, ...]:
        """Run after every price update: a loss-limit breach engages the kill switch at once."""
        now = self.clock()
        breaches = self.engine.account_breaches(self.account_view(now))
        if breaches and not self.kill_switch.engaged():
            self._trip(breaches, now)
        return breaches

    def submit(self, intent: OrderIntent, claims: TaskTokenClaims) -> ExecutionResult:
        now = self.clock()
        trace = uuid4().hex
        request = ActionRequest(
            task_id=claims.task_id,
            domain=Domain.TRADING,
            tool=TOOL_PLACE_ORDER,
            parameters=intent.model_dump(mode="json", exclude={"reason"}),
            tier=ApprovalTier.B,  # paper money; a live broker makes this tier D
            taint=claims.taint,
            rationale=intent.reason,
        )
        gate = decide(request, claims)
        allowed = gate.outcome is GateOutcome.EXECUTE
        self._audit(
            claims,
            trace,
            AuditAction.GATE_DECISION,
            f"{TOOL_PLACE_ORDER}:{gate.outcome}",
            AuditDecision.ALLOW if allowed else AuditDecision.DENY,
            request,
        )
        if not allowed:
            return ExecutionResult(intent, Outcome.REJECTED_GATE, detail=gate.reason)

        try:
            quote = self.broker.get_quote(intent.instrument)
        except BrokerError as exc:
            return ExecutionResult(intent, Outcome.REJECTED_BROKER, detail=str(exc))
        if intent.order_type is OrderType.LIMIT:
            assert intent.limit_price is not None
            price = intent.limit_price
        else:
            price = quote.ask if intent.side is Side.BUY else quote.bid
        check = OrderCheck(
            instrument=intent.instrument,
            side=intent.side,
            quantity=intent.quantity,
            price=price,
            quote_mid=quote.mid,
            quote_ts=quote.ts,
        )
        risk = self.engine.check(
            check,
            self.account_view(now),
            now=now,
            kill_switch_engaged=self.kill_switch.engaged(),
        )
        if risk.trip_kill_switch and not self.kill_switch.engaged():
            self._trip(risk.violations, now)
        codes = ",".join(sorted({v.code.value for v in risk.violations})) or "ok"
        self._audit(
            claims,
            trace,
            AuditAction.GATE_DECISION,
            f"trading_risk:{codes}"[:200],
            AuditDecision.ALLOW if risk.approved else AuditDecision.DENY,
            request,
            policy_id="trading_risk",
        )
        if not risk.approved:
            return ExecutionResult(intent, Outcome.REJECTED_RISK, violations=risk.violations)

        # Last look: the switch can be engaged from outside between the check and the send.
        if self.kill_switch.engaged():
            return ExecutionResult(intent, Outcome.REJECTED_RISK, detail="kill switch engaged")
        self.tracker.record_order(now)
        order = self.broker.place_order(
            OrderRequest(
                instrument=intent.instrument,
                side=intent.side,
                quantity=intent.quantity,
                order_type=intent.order_type,
                limit_price=intent.limit_price,
            )
        )
        placed = order.status is not OrderStatus.REJECTED
        self._audit(
            claims,
            trace,
            AuditAction.TOOL_CALL,
            f"{TOOL_PLACE_ORDER}:{order.status}",
            AuditDecision.ALLOW if placed else AuditDecision.DENY,
            request,
            content_ref=f"paper:{order.order_id}",
        )
        if not placed:
            return ExecutionResult(
                intent, Outcome.REJECTED_BROKER, order=order, detail=order.reject_reason or ""
            )
        outcome = Outcome.FILLED if order.status is OrderStatus.FILLED else Outcome.OPEN
        return ExecutionResult(intent, outcome, order=order)

    def cancel_all(self) -> list[Order]:
        return [self.broker.cancel_order(o.order_id) for o in self.broker.open_orders()]

    def note(self, target: str) -> None:
        """Audit a session-level state change (start, end) with no task attached."""
        self.audit(
            AuditLogEntry(
                trace_id=uuid4().hex,
                domain=Domain.TRADING,
                actor=ACTOR,
                action=AuditAction.STATE_TRANSITION,
                target=target[:200],
                decision=AuditDecision.ALLOW,
            )
        )

    def _trip(self, violations: tuple[Violation, ...], now: datetime) -> None:
        codes = ",".join(sorted({v.code.value for v in violations}))
        self.kill_switch.engage(f"risk limit breached: {codes}", now=now)
        self.cancel_all()
        self.audit(
            AuditLogEntry(
                trace_id=uuid4().hex,
                domain=Domain.TRADING,
                actor=ACTOR,
                action=AuditAction.STATE_TRANSITION,
                target=f"kill_switch:engaged:{codes}"[:200],
                decision=AuditDecision.DENY,
                policy_id="trading_risk",
            )
        )

    def _audit(
        self,
        claims: TaskTokenClaims,
        trace: str,
        action: AuditAction,
        target: str,
        decision: AuditDecision,
        request: ActionRequest,
        *,
        policy_id: str | None = None,
        content_ref: str | None = None,
    ) -> None:
        self.audit(
            AuditLogEntry(
                trace_id=trace,
                task_id=claims.task_id,
                domain=Domain.TRADING,
                actor=ACTOR,
                action=action,
                target=target,
                decision=decision,
                policy_id=policy_id,
                taint=claims.taint,
                content_hash=_hash(request.model_dump_json()),
                content_ref=content_ref or f"action:{request.request_id}",
            )
        )
