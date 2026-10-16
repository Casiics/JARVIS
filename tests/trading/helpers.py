"""Shared builders for the trading tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from jarvis_core.audit.chain import AuditWriter, InMemoryAuditStore
from jarvis_core.contracts.audit import AuditLogEntry
from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.events import Taint
from jarvis_core.contracts.task_token import TaskTokenClaims
from mod_trading.account import EquityTracker
from mod_trading.broker import PaperBroker
from mod_trading.executor import TRADING_TOOLS, OrderExecutor
from mod_trading.schemas import Quote
from trading_risk import KillSwitch, RiskEngine, RiskLimits

T0 = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)


def limits(**overrides: object) -> RiskLimits:
    base: dict[str, object] = {
        "instruments": frozenset({"SPY", "QQQ"}),
        "max_order_notional": Decimal(1000),
        "max_position_notional": Decimal(2500),
        "max_gross_exposure": Decimal(4000),
        "max_daily_loss": Decimal(200),
        "max_drawdown": Decimal("0.10"),
        "max_orders_per_minute": 3,
        "max_orders_per_day": 10,
        "max_price_deviation": Decimal("0.02"),
        "max_quote_age_s": 120,
    }
    base.update(overrides)
    return RiskLimits.model_validate(base)


def claims(*, tools: frozenset[str] = TRADING_TOOLS, taint: Taint = Taint.NONE) -> TaskTokenClaims:
    return TaskTokenClaims(
        task_id=uuid4(),
        domain=Domain.TRADING,
        allowed_tools=tools,
        budget_eur=0,
        taint=taint,
        expires_at=T0 + timedelta(days=3650),
    )


def quote(instrument: str, mid: str, ts: datetime = T0, spread: str = "0.02") -> Quote:
    m, s = Decimal(mid), Decimal(spread)
    return Quote(instrument=instrument, bid=m - s / 2, ask=m + s / 2, ts=ts)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Rig:
    """A paper broker, risk engine, kill switch and executor wired together for a test."""

    def __init__(self, tmp: Path, **limit_overrides: object) -> None:
        self.clock = Clock()
        self.entries: list[AuditLogEntry] = []
        writer = AuditWriter(InMemoryAuditStore())

        def audit(entry: AuditLogEntry) -> object:
            self.entries.append(entry)
            return writer.write(entry)

        self.broker = PaperBroker(
            starting_cash=Decimal(10_000),
            slippage_bps=Decimal(0),
            fee_bps=Decimal(0),
            clock=self.clock,
        )
        self.kill_switch = KillSwitch(tmp / "killswitch")
        self.kill_switch.release()
        self.executor = OrderExecutor(
            broker=self.broker,
            engine=RiskEngine(limits(**limit_overrides)),
            kill_switch=self.kill_switch,
            tracker=EquityTracker(UTC),
            audit=audit,
            clock=self.clock,
        )

    def price(self, instrument: str, mid: str) -> None:
        self.broker.update_quote(quote(instrument, mid, self.clock.now))
