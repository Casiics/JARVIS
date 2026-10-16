"""The risk engine: every limit, alone and together."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.trading.helpers import T0, limits
from trading_risk import AccountView, OrderCheck, RiskEngine, Side, ViolationCode

ENGINE = RiskEngine(limits())


def order(**overrides: object) -> OrderCheck:
    base: dict[str, object] = {
        "instrument": "SPY",
        "side": Side.BUY,
        "quantity": Decimal(1),
        "price": Decimal(500),
        "quote_mid": Decimal(500),
        "quote_ts": T0,
    }
    base.update(overrides)
    return OrderCheck.model_validate(base)


def account(**overrides: object) -> AccountView:
    base: dict[str, object] = {
        "equity": Decimal(10_000),
        "day_start_equity": Decimal(10_000),
        "peak_equity": Decimal(10_000),
    }
    base.update(overrides)
    return AccountView.model_validate(base)


def codes(check: OrderCheck, view: AccountView, *, engaged: bool = False) -> set[ViolationCode]:
    decision = ENGINE.check(check, view, now=T0, kill_switch_engaged=engaged)
    assert decision.approved == (not decision.violations)
    return {v.code for v in decision.violations}


def test_a_plain_order_passes() -> None:
    assert codes(order(), account()) == set()


@pytest.mark.parametrize(
    ("check", "view", "expected"),
    [
        (order(instrument="TSLA"), account(), ViolationCode.INSTRUMENT),
        (order(quantity=Decimal(3)), account(), ViolationCode.ORDER_NOTIONAL),
        (
            order(),
            account(positions={"SPY": Decimal(5)}, marks={"SPY": Decimal(500)}),
            ViolationCode.POSITION_NOTIONAL,
        ),
        (
            order(),
            account(positions={"QQQ": Decimal(8)}, marks={"QQQ": Decimal(450)}),
            ViolationCode.GROSS_EXPOSURE,
        ),
        (order(side=Side.SELL), account(), ViolationCode.SHORT),
        (order(price=Decimal(520)), account(), ViolationCode.PRICE_DEVIATION),
        (order(quote_ts=T0 - timedelta(minutes=5)), account(), ViolationCode.STALE_QUOTE),
        (order(quote_ts=T0 + timedelta(minutes=1)), account(), ViolationCode.STALE_QUOTE),
        (order(), account(orders_sent=(T0,) * 3), ViolationCode.FREQUENCY_MINUTE),
        (
            order(),
            account(orders_sent=(T0 - timedelta(hours=1),) * 10),
            ViolationCode.FREQUENCY_DAY,
        ),
        (order(), account(equity=Decimal(9_800)), ViolationCode.DAILY_LOSS),
        (
            order(),
            account(
                equity=Decimal(9_000), day_start_equity=Decimal(9_100), peak_equity=Decimal(10_000)
            ),
            ViolationCode.DRAWDOWN,
        ),
        (
            order(),
            account(positions={"QQQ": Decimal(1)}),
            ViolationCode.MISSING_MARK,
        ),
    ],
)
def test_each_limit_denies(check: OrderCheck, view: AccountView, expected: ViolationCode) -> None:
    assert expected in codes(check, view)


def test_kill_switch_denies_everything() -> None:
    assert codes(order(), account(), engaged=True) == {ViolationCode.KILL_SWITCH}


def test_selling_a_held_position_is_allowed() -> None:
    view = account(positions={"SPY": Decimal(2)}, marks={"SPY": Decimal(500)})
    assert codes(order(side=Side.SELL, quantity=Decimal(2)), view) == set()


def test_loss_limits_ask_to_trip_the_kill_switch() -> None:
    decision = ENGINE.check(
        order(), account(equity=Decimal(9_700)), now=T0, kill_switch_engaged=False
    )
    assert not decision.approved and decision.trip_kill_switch
    other = ENGINE.check(order(instrument="TSLA"), account(), now=T0, kill_switch_engaged=False)
    assert not other.approved and not other.trip_kill_switch


def test_violations_are_all_reported() -> None:
    found = codes(order(instrument="TSLA", quantity=Decimal(10)), account(equity=Decimal(9_000)))
    assert {
        ViolationCode.INSTRUMENT,
        ViolationCode.ORDER_NOTIONAL,
        ViolationCode.DAILY_LOSS,
    } <= found


def test_short_selling_needs_an_explicit_limit() -> None:
    engine = RiskEngine(limits(allow_short=True))
    decision = engine.check(order(side=Side.SELL), account(), now=T0, kill_switch_engaged=False)
    assert decision.approved


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_order_notional": Decimal(3000)},
        {"max_position_notional": Decimal(5000)},
        {"max_orders_per_minute": 11},
        {"max_drawdown": Decimal("1.5")},
        {"instruments": frozenset()},
        {"max_daily_loss": Decimal(0)},
        {"unknown": 1},
    ],
)
def test_inconsistent_limits_are_refused(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        limits(**overrides)


def test_shipped_limits_file_is_valid() -> None:
    from mod_trading.__main__ import DEFAULT_LIMITS, load_limits

    shipped = load_limits(DEFAULT_LIMITS)
    assert not shipped.allow_short
    assert shipped.max_drawdown <= Decimal("0.2")
