"""Deterministic strategies. They see bars and the account and propose order intents; they
never place orders, and nothing they return bypasses the executor's gate and risk checks.
"""

from __future__ import annotations

import itertools
import math
from collections import deque
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Protocol

from mod_trading.schemas import Bar, OrderIntent, Side
from trading_risk import AccountView


class Strategy(Protocol):
    name: str

    def on_bar(self, bar: Bar, account: AccountView) -> list[OrderIntent]: ...


@dataclass
class SmaCrossover:
    """Long-only moving-average crossover on one instrument.

    Buys `notional` worth when the fast average crosses above the slow one and the position is
    flat; sells the whole position when it crosses back below.
    """

    instrument: str
    fast: int = 10
    slow: int = 30
    notional: Decimal = Decimal(1000)
    quantity_step: Decimal = Decimal(1)
    name: str = "sma_crossover"
    _closes: deque[Decimal] = field(init=False)
    _above: bool | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if not 0 < self.fast < self.slow:
            raise ValueError("need 0 < fast < slow")
        self._closes = deque(maxlen=self.slow)

    def on_bar(self, bar: Bar, account: AccountView) -> list[OrderIntent]:
        if bar.instrument != self.instrument:
            return []
        self._closes.append(bar.close)
        if len(self._closes) < self.slow:
            return []
        closes = list(self._closes)
        fast = sum(closes[-self.fast :], Decimal(0)) / self.fast
        slow = sum(closes, Decimal(0)) / self.slow
        above = fast > slow
        crossed = self._above is not None and above != self._above
        self._above = above
        if not crossed:
            return []
        held = account.positions.get(self.instrument, Decimal(0))
        if above and held == 0:
            quantity = (self.notional / bar.close).quantize(self.quantity_step, ROUND_DOWN)
            if quantity <= 0:
                return []
            return [self._intent(Side.BUY, quantity, f"fast SMA {fast:.4f} above slow {slow:.4f}")]
        if not above and held > 0:
            return [self._intent(Side.SELL, held, f"fast SMA {fast:.4f} below slow {slow:.4f}")]
        return []

    def _intent(self, side: Side, quantity: Decimal, reason: str) -> OrderIntent:
        return OrderIntent(
            strategy=self.name,
            instrument=self.instrument,
            side=side,
            quantity=quantity,
            reason=reason,
        )


@dataclass
class TrendVolTarget:
    """Long-only trend filter with volatility targeting on one instrument.

    Invested only while the close is above its `trend`-bar average. While invested, the
    position aims at `capital * min(max_weight, target_vol / realised_vol)`, where realised
    volatility is the annualised standard deviation of the last `vol_window` log returns. Calm
    markets get the full position, turbulent ones a smaller one, a broken trend none.

    Rebalances only when the gap to the target exceeds `band` of `capital`, to avoid churn.
    Orders are split so none exceeds `max_order_notional`, at most `max_orders_per_bar` per bar,
    which keeps it inside the risk limits it trades under (an order the limits refuse would
    still be refused; this only avoids proposing it). Leave `capital` a little below the
    position limit, since fills happen at the ask, not at the close.
    """

    instrument: str
    trend: int = 200
    vol_window: int = 20
    target_vol: float = 0.10
    capital: Decimal = Decimal(2400)
    max_weight: float = 1.0
    band: Decimal = Decimal("0.10")
    max_order_notional: Decimal = Decimal(1000)
    max_orders_per_bar: int = 3
    quantity_step: Decimal = Decimal(1)
    bars_per_year: int = 252
    name: str = "trend_vol_target"
    _closes: deque[Decimal] = field(init=False)

    def __post_init__(self) -> None:
        if self.trend < 2 or self.vol_window < 2 or self.vol_window >= self.trend:
            raise ValueError("need 2 <= vol_window < trend")
        if not 0 < self.target_vol <= 1 or not 0 < self.max_weight <= 1:
            raise ValueError("target_vol and max_weight must be in (0, 1]")
        self._closes = deque(maxlen=self.trend)

    def target_weight(self) -> float | None:
        """The weight the strategy wants now, or None while it is still warming up."""
        if len(self._closes) < self.trend:
            return None
        closes = list(self._closes)
        if closes[-1] <= sum(closes, Decimal(0)) / len(closes):
            return 0.0
        window = [float(c) for c in closes[-(self.vol_window + 1) :]]
        returns = [math.log(b / a) for a, b in itertools.pairwise(window)]
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        vol = math.sqrt(variance * self.bars_per_year)
        return self.max_weight if vol == 0 else min(self.max_weight, self.target_vol / vol)

    def on_bar(self, bar: Bar, account: AccountView) -> list[OrderIntent]:
        if bar.instrument != self.instrument:
            return []
        self._closes.append(bar.close)
        weight = self.target_weight()
        if weight is None:
            return []
        price = bar.close
        held = account.positions.get(self.instrument, Decimal(0))
        target = (self.capital * Decimal(str(weight)) / price).quantize(
            self.quantity_step, ROUND_DOWN
        )
        gap = target - held
        if gap == 0 or (target > 0 and abs(gap) * price < self.band * self.capital):
            return []
        side = Side.BUY if gap > 0 else Side.SELL
        per_order = (self.max_order_notional / price).quantize(self.quantity_step, ROUND_DOWN)
        if per_order <= 0:
            return []
        reason = f"weight {weight:.2f}, target {target}, held {held}"
        intents: list[OrderIntent] = []
        remaining = abs(gap)
        while remaining > 0 and len(intents) < self.max_orders_per_bar:
            quantity = min(remaining, per_order)
            intents.append(
                OrderIntent(
                    strategy=self.name,
                    instrument=self.instrument,
                    side=side,
                    quantity=quantity,
                    reason=reason,
                )
            )
            remaining -= quantity
        return intents
