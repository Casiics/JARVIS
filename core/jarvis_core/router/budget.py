"""FinOps circuit breaker: global and per-domain spend limits per hour and per day."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from jarvis_core.contracts.classification import Domain


class BudgetExceeded(Exception):
    pass


@dataclass(frozen=True)
class BudgetLimits:
    hourly_eur: float = 2.50
    daily_eur: float = 15.00
    per_domain_daily_eur: dict[Domain, float] = field(
        default_factory=lambda: {
            Domain.CORE: 6.00,
            Domain.TRADING: 3.00,
            Domain.CONTENT: 6.00,
            Domain.PRIVATE: 0.00,  # local only, so any cloud spend is a policy breach
        }
    )


class SpendLedger(Protocol):
    def spent(self, since: datetime, domain: Domain | None = None) -> float: ...

    def record(self, at: datetime, domain: Domain, eur: float) -> None: ...


class InMemorySpendLedger:
    """Development ledger. Production uses Redis with the same interface."""

    def __init__(self) -> None:
        self._items: dict[Domain, list[tuple[datetime, float]]] = defaultdict(list)

    def spent(self, since: datetime, domain: Domain | None = None) -> float:
        domains = [domain] if domain else list(self._items)
        return sum(eur for d in domains for at, eur in self._items[d] if at >= since)

    def record(self, at: datetime, domain: Domain, eur: float) -> None:
        self._items[domain].append((at, eur))


class CircuitBreaker:
    def __init__(self, ledger: SpendLedger, limits: BudgetLimits | None = None) -> None:
        self._ledger = ledger
        self._limits = limits or BudgetLimits()

    def check(self, domain: Domain, estimated_eur: float, now: datetime) -> None:
        """Raise BudgetExceeded if the estimated call would break any limit."""
        hour = self._ledger.spent(now - timedelta(hours=1)) + estimated_eur
        day = self._ledger.spent(now - timedelta(days=1)) + estimated_eur
        domain_day = self._ledger.spent(now - timedelta(days=1), domain) + estimated_eur
        if hour > self._limits.hourly_eur:
            raise BudgetExceeded(f"hourly budget {self._limits.hourly_eur} EUR exceeded")
        if day > self._limits.daily_eur:
            raise BudgetExceeded(f"daily budget {self._limits.daily_eur} EUR exceeded")
        limit = self._limits.per_domain_daily_eur.get(domain, 0.0)
        if estimated_eur > 0 and domain_day > limit:
            raise BudgetExceeded(f"daily budget for {domain} ({limit} EUR) exceeded")

    def record(self, domain: Domain, eur: float, now: datetime) -> None:
        self._ledger.record(now, domain, eur)
