"""Deterministic strategies. They see bars and the account and propose order intents; they
never place orders, and nothing they return bypasses the executor's gate and risk checks.
"""

from __future__ import annotations

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
