"""The paper broker: fills, fees, slippage, resting limits, rejections and persistence."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from mod_trading.broker import BrokerError, PaperBroker
from mod_trading.schemas import OrderRequest, OrderStatus, OrderType, Side
from tests.trading.helpers import Clock, quote


def broker(**kwargs: object) -> PaperBroker:
    b = PaperBroker(clock=Clock(), **kwargs)  # type: ignore[arg-type]
    b.update_quote(quote("SPY", "500", spread="0.10"))
    return b


def market(side: Side, qty: str = "2") -> OrderRequest:
    return OrderRequest(
        instrument="SPY", side=side, quantity=Decimal(qty), order_type=OrderType.MARKET
    )


def test_market_buy_pays_ask_slippage_and_fee() -> None:
    b = broker(slippage_bps=Decimal(10), fee_bps=Decimal(10))
    order = b.place_order(market(Side.BUY))
    assert order.status is OrderStatus.FILLED
    price = Decimal("500.05") * Decimal("1.001")
    assert order.avg_price == price.quantize(Decimal("0.0001"))
    fee = (order.avg_price * 2 * Decimal("0.001")).quantize(Decimal("0.0001"))
    assert b.state.cash == Decimal(10_000) - order.avg_price * 2 - fee
    assert b.get_positions() == {"SPY": Decimal(2)}


def test_round_trip_loses_spread_and_fees() -> None:
    b = broker(slippage_bps=Decimal(5), fee_bps=Decimal(10))
    b.place_order(market(Side.BUY))
    b.place_order(market(Side.SELL))
    assert b.get_positions() == {}
    assert b.state.cash < Decimal(10_000)
    assert b.equity({}) == b.state.cash


def test_limit_order_rests_until_the_quote_crosses() -> None:
    b = broker()
    order = b.place_order(
        OrderRequest(
            instrument="SPY",
            side=Side.BUY,
            quantity=Decimal(1),
            order_type=OrderType.LIMIT,
            limit_price=Decimal(490),
        )
    )
    assert order.status is OrderStatus.OPEN
    assert b.update_quote(quote("SPY", "495")) == []
    filled = b.update_quote(quote("SPY", "489"))
    assert [o.status for o in filled] == [OrderStatus.FILLED]
    assert filled[0].avg_price is not None and filled[0].avg_price <= Decimal(490)


def test_cancel_open_order() -> None:
    b = broker()
    order = b.place_order(
        OrderRequest(
            instrument="SPY",
            side=Side.BUY,
            quantity=Decimal(1),
            order_type=OrderType.LIMIT,
            limit_price=Decimal(400),
        )
    )
    assert b.cancel_order(order.order_id).status is OrderStatus.CANCELLED
    assert b.open_orders() == []
    with pytest.raises(BrokerError):
        b.cancel_order("paper-999")


@pytest.mark.parametrize(
    ("request_", "reason"),
    [
        (market(Side.BUY, "100"), "insufficient cash"),
        (market(Side.SELL, "1"), "insufficient position"),
        (
            OrderRequest(
                instrument="QQQ", side=Side.BUY, quantity=Decimal(1), order_type=OrderType.MARKET
            ),
            "no quote",
        ),
    ],
)
def test_rejections(request_: OrderRequest, reason: str) -> None:
    order = broker().place_order(request_)
    assert order.status is OrderStatus.REJECTED and order.reject_reason == reason


def test_state_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "paper.json"
    b = broker(state_path=path)
    b.place_order(market(Side.BUY))
    again = PaperBroker(state_path=path, clock=Clock())
    assert again.get_positions() == {"SPY": Decimal(2)}
    assert again.state.cash == b.state.cash
    assert len(again.state.fills) == 1
