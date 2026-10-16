"""Broker adapters. Phase 6 has exactly one: the paper broker.

`BrokerAdapter` is the interface every broker implements (decision 10 in docs/decisions.md).
There is no withdrawal, transfer or key management in it, by design. A live adapter arrives
only after weeks of paper trading and a human decision; the executor refuses any adapter
whose mode is not "paper" until then.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from mod_trading.schemas import Fill, Order, OrderRequest, OrderStatus, OrderType, Quote, Side
from mod_trading.store import write_atomic

BrokerMode = Literal["paper", "live"]
CENT = Decimal("0.0001")


class BrokerError(Exception):
    pass


class BrokerAdapter(Protocol):
    @property
    def mode(self) -> BrokerMode: ...

    def get_quote(self, instrument: str) -> Quote: ...

    def get_positions(self) -> dict[str, Decimal]: ...

    def place_order(self, request: OrderRequest) -> Order: ...

    def cancel_order(self, order_id: str) -> Order: ...


class PaperState(BaseModel):
    """Everything the paper broker knows, persisted after every change."""

    model_config = ConfigDict(extra="forbid")

    cash: Decimal
    positions: dict[str, Decimal] = Field(default_factory=dict)
    orders: dict[str, Order] = Field(default_factory=dict)
    fills: list[Fill] = Field(default_factory=list)
    fees_paid: Decimal = Decimal(0)
    sequence: int = 0


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


@dataclass
class PaperBroker:
    """Simulated broker. Market orders fill at the quote's far side plus slippage; limit orders
    fill when the quote crosses them and otherwise rest until `update_quote` or a cancel.

    Fees and slippage are basis points of the fill's notional. Orders that the cash or the
    position cannot cover are rejected, as a cash account at a real broker would.
    """

    starting_cash: Decimal = Decimal(10_000)
    slippage_bps: Decimal = Decimal(5)
    fee_bps: Decimal = Decimal(10)
    state_path: Path | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    quotes: dict[str, Quote] = field(default_factory=dict)
    state: PaperState = field(init=False)

    def __post_init__(self) -> None:
        if self.state_path is not None and self.state_path.exists():
            self.state = PaperState.model_validate_json(self.state_path.read_text("utf-8"))
        else:
            self.state = PaperState(cash=self.starting_cash)

    @property
    def mode(self) -> BrokerMode:
        return "paper"

    # --- BrokerAdapter ------------------------------------------------------------------------

    def get_quote(self, instrument: str) -> Quote:
        quote = self.quotes.get(instrument)
        if quote is None:
            raise BrokerError(f"no quote for {instrument}")
        return quote

    def get_positions(self) -> dict[str, Decimal]:
        return {k: v for k, v in self.state.positions.items() if v != 0}

    def place_order(self, request: OrderRequest) -> Order:
        now = self.clock()
        self.state.sequence += 1
        order = Order(
            order_id=f"paper-{self.state.sequence}",
            request=request,
            status=OrderStatus.OPEN,
            created_at=now,
            updated_at=now,
        )
        quote = self.quotes.get(request.instrument)
        if quote is None:
            order = self._reject(order, "no quote", now)
        else:
            order = self._try_fill(order, quote, now)
        self.state.orders[order.order_id] = order
        self._save()
        return order

    def cancel_order(self, order_id: str) -> Order:
        order = self.state.orders.get(order_id)
        if order is None:
            raise BrokerError(f"unknown order {order_id}")
        if order.status is OrderStatus.OPEN:
            order = order.model_copy(
                update={"status": OrderStatus.CANCELLED, "updated_at": self.clock()}
            )
            self.state.orders[order_id] = order
            self._save()
        return order

    # --- paper specifics -----------------------------------------------------------------------

    def update_quote(self, quote: Quote) -> list[Order]:
        """New market data: remember it and fill resting limit orders it crosses."""
        self.quotes[quote.instrument] = quote
        changed: list[Order] = []
        for order_id, order in list(self.state.orders.items()):
            if order.status is OrderStatus.OPEN and order.request.instrument == quote.instrument:
                updated = self._try_fill(order, quote, quote.ts)
                if updated.status is not OrderStatus.OPEN:
                    self.state.orders[order_id] = updated
                    changed.append(updated)
        if changed:
            self._save()
        return changed

    def open_orders(self) -> list[Order]:
        return [o for o in self.state.orders.values() if o.status is OrderStatus.OPEN]

    def equity(self, marks: dict[str, Decimal]) -> Decimal:
        value = self.state.cash
        for instrument, quantity in self.state.positions.items():
            if quantity == 0:
                continue
            mark = marks.get(instrument)
            if mark is None:
                raise BrokerError(f"no mark for {instrument}")
            value += quantity * mark
        return _money(value)

    def _try_fill(self, order: Order, quote: Quote, now: datetime) -> Order:
        request = order.request
        buy = request.side is Side.BUY
        touch = quote.ask if buy else quote.bid
        if request.order_type is OrderType.LIMIT:
            assert request.limit_price is not None
            crosses = touch <= request.limit_price if buy else touch >= request.limit_price
            if not crosses:
                return order
            price = touch
        else:
            slip = touch * self.slippage_bps / Decimal(10_000)
            price = touch + slip if buy else touch - slip
        price = _money(price)
        notional = price * request.quantity
        fee = _money(notional * self.fee_bps / Decimal(10_000))
        held = self.state.positions.get(request.instrument, Decimal(0))
        if buy and notional + fee > self.state.cash:
            return self._reject(order, "insufficient cash", now)
        if not buy and request.quantity > held:
            return self._reject(order, "insufficient position", now)
        self.state.cash = _money(
            self.state.cash - notional - fee if buy else self.state.cash + notional - fee
        )
        self.state.positions[request.instrument] = (
            held + request.quantity if buy else held - request.quantity
        )
        self.state.fees_paid += fee
        self.state.fills.append(
            Fill(
                order_id=order.order_id,
                instrument=request.instrument,
                side=request.side,
                quantity=request.quantity,
                price=price,
                fee=fee,
                ts=now,
            )
        )
        return order.model_copy(
            update={
                "status": OrderStatus.FILLED,
                "filled_quantity": request.quantity,
                "avg_price": price,
                "updated_at": now,
            }
        )

    @staticmethod
    def _reject(order: Order, reason: str, now: datetime) -> Order:
        return order.model_copy(
            update={"status": OrderStatus.REJECTED, "reject_reason": reason, "updated_at": now}
        )

    def _save(self) -> None:
        if self.state_path is not None:
            write_atomic(self.state_path, json.dumps(self.state.model_dump(mode="json")))
