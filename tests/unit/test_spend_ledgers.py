"""Durable FinOps ledgers: counters survive a restart and an unreadable ledger fails closed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jarvis_core.contracts.classification import Domain
from jarvis_core.router.budget import (
    BudgetExceeded,
    CircuitBreaker,
    FileSpendLedger,
    LedgerCorrupt,
    RedisSpendLedger,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def test_file_ledger_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "spend.jsonl"
    CircuitBreaker(FileSpendLedger(path)).record(Domain.CONTENT, 2.40, NOW - timedelta(minutes=5))
    restarted = CircuitBreaker(FileSpendLedger(path))
    with pytest.raises(BudgetExceeded, match="hourly"):
        restarted.check(Domain.CONTENT, 0.20, NOW)


def test_file_ledger_is_shared_between_domains_for_the_global_limit(tmp_path: Path) -> None:
    path = tmp_path / "spend.jsonl"
    FileSpendLedger(path).record(NOW - timedelta(hours=3), Domain.CONTENT, 6.00)
    FileSpendLedger(path).record(NOW - timedelta(hours=3), Domain.CORE, 6.00)
    FileSpendLedger(path).record(NOW - timedelta(hours=3), Domain.TRADING, 2.00)
    ledger = FileSpendLedger(path)
    assert ledger.spent(NOW - timedelta(days=1)) == pytest.approx(14.0)
    assert ledger.spent(NOW - timedelta(days=1), Domain.CORE) == pytest.approx(6.0)
    with pytest.raises(BudgetExceeded, match="daily budget 15"):
        CircuitBreaker(ledger).check(Domain.TRADING, 1.50, NOW)


def test_file_ledger_is_private_to_its_owner(tmp_path: Path) -> None:
    path = tmp_path / "spend.jsonl"
    FileSpendLedger(path).record(NOW, Domain.CORE, 0.1)
    assert path.stat().st_mode & 0o777 == 0o600


def test_unreadable_ledger_stops_spending(tmp_path: Path) -> None:
    path = tmp_path / "spend.jsonl"
    FileSpendLedger(path).record(NOW, Domain.CORE, 0.1)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"at": "yesterday", "domain": "core", "eur": 1}\n')
    breaker = CircuitBreaker(FileSpendLedger(path))
    with pytest.raises(LedgerCorrupt):
        breaker.check(Domain.CORE, 0.0, NOW)


def test_negative_or_naive_entries_are_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "spend.jsonl"
    path.write_text('{"at": "2026-10-16T11:00:00", "domain": "core", "eur": 1}\n')
    with pytest.raises(LedgerCorrupt):
        FileSpendLedger(path).spent(NOW - timedelta(days=1))
    path.write_text('{"at": "2026-10-16T11:00:00+00:00", "domain": "core", "eur": -5}\n')
    with pytest.raises(LedgerCorrupt):
        FileSpendLedger(path).spent(NOW - timedelta(days=1))


def test_compact_keeps_only_what_can_still_count(tmp_path: Path) -> None:
    ledger = FileSpendLedger(tmp_path / "spend.jsonl")
    ledger.record(NOW - timedelta(days=5), Domain.CORE, 1.0)
    ledger.record(NOW - timedelta(hours=1), Domain.CORE, 0.5)
    assert ledger.compact(NOW) == 1
    assert ledger.spent(NOW - timedelta(days=30)) == pytest.approx(0.5)


class FakeRedis:
    def __init__(self) -> None:
        self.sets: dict[str, dict[str, float]] = {}

    def zadd(self, name: str, mapping: dict[str, float]) -> int:
        self.sets.setdefault(name, {}).update(mapping)
        return len(mapping)

    def zrangebyscore(self, name: str, min: float, max: str) -> list[bytes]:
        assert max == "+inf"
        return [m.encode() for m, score in self.sets.get(name, {}).items() if score >= min]

    def zremrangebyscore(self, name: str, min: str, max: float) -> int:
        assert min == "-inf"
        members = self.sets.get(name, {})
        old = [m for m, score in members.items() if score <= max]
        for m in old:
            del members[m]
        return len(old)


def test_redis_ledger_counts_equal_amounts_separately_and_trims() -> None:
    redis = FakeRedis()
    ledger = RedisSpendLedger(redis)
    ledger.record(NOW - timedelta(days=3), Domain.CONTENT, 1.0)
    ledger.record(NOW, Domain.CONTENT, 0.25)
    ledger.record(NOW, Domain.CONTENT, 0.25)
    ledger.record(NOW, Domain.TRADING, 1.0)
    assert len(redis.sets["jarvis:finops:spend:content"]) == 2  # the 3-day-old entry is gone
    assert ledger.spent(NOW - timedelta(hours=1), Domain.CONTENT) == pytest.approx(0.5)
    assert ledger.spent(NOW - timedelta(hours=1)) == pytest.approx(1.5)


def test_redis_ledger_fails_closed_on_foreign_members() -> None:
    redis = FakeRedis()
    redis.zadd("jarvis:finops:spend:core", {"garbage": NOW.timestamp()})
    with pytest.raises(LedgerCorrupt):
        RedisSpendLedger(redis).spent(NOW - timedelta(hours=1))
