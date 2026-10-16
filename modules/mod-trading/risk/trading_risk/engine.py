"""Pre-trade risk checks: plain code, no model, no I/O.

The engine answers one question: may this order go to the broker now? It checks the order
against fixed limits and the account's current state and returns every violation it finds.
Breaching the daily loss or drawdown limit also asks the caller to engage the kill switch.
Money is Decimal throughout; floats never enter a limit comparison.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Positive = Annotated[Decimal, Field(gt=0)]
NonNegative = Annotated[Decimal, Field(ge=0)]
Fraction = Annotated[Decimal, Field(gt=0, lt=1)]


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class RiskLimits(BaseModel):
    """Fixed per account. Loaded from `modules/mod-trading/risk/limits.json` by the executor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instruments: frozenset[str] = Field(min_length=1, description="instrument allowlist")
    max_order_notional: Positive
    max_position_notional: Positive = Field(description="per instrument, absolute")
    max_gross_exposure: Positive = Field(description="sum of absolute position values")
    max_daily_loss: Positive = Field(description="equity drop since the day's start")
    max_drawdown: Fraction = Field(description="equity drop from its peak, as a fraction")
    max_orders_per_minute: int = Field(ge=1)
    max_orders_per_day: int = Field(ge=1)
    max_price_deviation: Fraction = Field(description="order price against the quote")
    max_quote_age_s: int = Field(ge=1)
    allow_short: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> RiskLimits:
        if self.max_order_notional > self.max_position_notional:
            raise ValueError("max_order_notional exceeds max_position_notional")
        if self.max_position_notional > self.max_gross_exposure:
            raise ValueError("max_position_notional exceeds max_gross_exposure")
        if self.max_orders_per_minute > self.max_orders_per_day:
            raise ValueError("max_orders_per_minute exceeds max_orders_per_day")
        return self


class OrderCheck(BaseModel):
    """The order as the executor is about to send it, with the quote it was priced against."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument: str = Field(min_length=1, max_length=40)
    side: Side
    quantity: Positive
    price: Positive = Field(description="limit price, or the quote side a market order meets")
    quote_mid: Positive
    quote_ts: AwareDatetime


class AccountView(BaseModel):
    """Account state at check time, computed by the executor from the broker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    equity: Decimal
    day_start_equity: Positive
    peak_equity: Positive
    positions: dict[str, Decimal] = Field(default_factory=dict, description="signed quantity")
    marks: dict[str, Positive] = Field(default_factory=dict, description="price per instrument")
    orders_sent: tuple[AwareDatetime, ...] = Field(
        default=(), description="send times of orders today, any order"
    )


class ViolationCode(StrEnum):
    KILL_SWITCH = "kill_switch"
    INSTRUMENT = "instrument_not_allowed"
    ORDER_NOTIONAL = "order_notional"
    POSITION_NOTIONAL = "position_notional"
    GROSS_EXPOSURE = "gross_exposure"
    SHORT = "short_not_allowed"
    PRICE_DEVIATION = "price_deviation"
    STALE_QUOTE = "stale_quote"
    FREQUENCY_MINUTE = "orders_per_minute"
    FREQUENCY_DAY = "orders_per_day"
    DAILY_LOSS = "daily_loss"
    DRAWDOWN = "drawdown"
    MISSING_MARK = "missing_mark"


TRIPPING = frozenset({ViolationCode.DAILY_LOSS, ViolationCode.DRAWDOWN})


class Violation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ViolationCode
    detail: str = Field(max_length=200)


class RiskDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    approved: bool
    violations: tuple[Violation, ...] = ()
    trip_kill_switch: bool = False


class RiskEngine:
    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits

    def account_breaches(self, account: AccountView) -> tuple[Violation, ...]:
        """Loss limits on their own, for the executor's check after every price update."""
        found: list[Violation] = []
        loss = account.day_start_equity - account.equity
        if loss >= self.limits.max_daily_loss:
            found.append(Violation(code=ViolationCode.DAILY_LOSS, detail=f"loss {loss}"))
        drawdown = (account.peak_equity - account.equity) / account.peak_equity
        if drawdown >= self.limits.max_drawdown:
            found.append(Violation(code=ViolationCode.DRAWDOWN, detail=f"drawdown {drawdown:.4f}"))
        return tuple(found)

    def check(
        self, order: OrderCheck, account: AccountView, *, now: datetime, kill_switch_engaged: bool
    ) -> RiskDecision:
        limits = self.limits
        found: list[Violation] = []

        def deny(code: ViolationCode, detail: str) -> None:
            found.append(Violation(code=code, detail=detail[:200]))

        if kill_switch_engaged:
            deny(ViolationCode.KILL_SWITCH, "kill switch engaged")
        if order.instrument not in limits.instruments:
            deny(ViolationCode.INSTRUMENT, order.instrument)

        notional = order.quantity * order.price
        if notional > limits.max_order_notional:
            deny(ViolationCode.ORDER_NOTIONAL, f"{notional} > {limits.max_order_notional}")

        signed = order.quantity if order.side is Side.BUY else -order.quantity
        current = account.positions.get(order.instrument, Decimal(0))
        after = current + signed
        if after < 0 and not limits.allow_short:
            deny(ViolationCode.SHORT, f"position would be {after}")
        if abs(after) * order.price > limits.max_position_notional:
            deny(ViolationCode.POSITION_NOTIONAL, f"{abs(after) * order.price}")

        gross = Decimal(0)
        for instrument, quantity in account.positions.items():
            if instrument == order.instrument or quantity == 0:
                continue
            mark = account.marks.get(instrument)
            if mark is None:
                deny(ViolationCode.MISSING_MARK, instrument)
                continue
            gross += abs(quantity) * mark
        gross += abs(after) * order.price
        if gross > limits.max_gross_exposure:
            deny(ViolationCode.GROSS_EXPOSURE, f"{gross} > {limits.max_gross_exposure}")

        deviation = abs(order.price - order.quote_mid) / order.quote_mid
        if deviation > limits.max_price_deviation:
            deny(ViolationCode.PRICE_DEVIATION, f"{deviation:.4f}")
        age = now - order.quote_ts
        if age > timedelta(seconds=limits.max_quote_age_s) or age < timedelta(seconds=-5):
            deny(ViolationCode.STALE_QUOTE, f"quote age {age.total_seconds():.0f}s")

        recent = [t for t in account.orders_sent if now - t < timedelta(minutes=1)]
        if len(recent) >= limits.max_orders_per_minute:
            deny(ViolationCode.FREQUENCY_MINUTE, f"{len(recent)} in the last minute")
        if len(account.orders_sent) >= limits.max_orders_per_day:
            deny(ViolationCode.FREQUENCY_DAY, f"{len(account.orders_sent)} today")

        breaches = self.account_breaches(account)
        found.extend(breaches)
        return RiskDecision(
            approved=not found,
            violations=tuple(found),
            trip_kill_switch=any(v.code in TRIPPING for v in breaches),
        )
