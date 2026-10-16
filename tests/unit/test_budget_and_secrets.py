from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jarvis_core.contracts.classification import Domain
from jarvis_core.router.budget import BudgetExceeded, CircuitBreaker, InMemorySpendLedger
from jarvis_core.secrets.provider import SecretAccessError, UnpackedSopsProvider

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def test_hourly_limit_trips() -> None:
    breaker = CircuitBreaker(InMemorySpendLedger())
    breaker.record(Domain.CONTENT, 2.40, NOW - timedelta(minutes=10))
    with pytest.raises(BudgetExceeded, match="hourly"):
        breaker.check(Domain.CONTENT, 0.20, NOW)


def test_old_spend_does_not_count_for_the_hour() -> None:
    breaker = CircuitBreaker(InMemorySpendLedger())
    breaker.record(Domain.CONTENT, 2.40, NOW - timedelta(hours=2))
    breaker.check(Domain.CONTENT, 0.20, NOW)


def test_private_domain_has_no_cloud_budget() -> None:
    breaker = CircuitBreaker(InMemorySpendLedger())
    with pytest.raises(BudgetExceeded, match="private"):
        breaker.check(Domain.PRIVATE, 0.01, NOW)


def test_provider_reads_only_own_domain(tmp_path: Path) -> None:
    (tmp_path / "trading").mkdir()
    (tmp_path / "trading" / "broker_key").write_text("abc\n")
    (tmp_path / "content").mkdir()
    (tmp_path / "content" / "ig_token").write_text("xyz")
    provider = UnpackedSopsProvider(Domain.TRADING, root=tmp_path)
    assert provider.get("trading/broker_key") == "abc"
    with pytest.raises(SecretAccessError):
        provider.get("content/ig_token")
    with pytest.raises(SecretAccessError):
        provider.get("trading/../content/ig_token")
