"""FinOps circuit breaker: global and per-domain spend limits per hour and per day."""

from __future__ import annotations

import fcntl
import json
import os
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, TextIO
from uuid import uuid4

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


# Spend older than this can no longer affect any limit (the longest window is one day).
RETENTION = timedelta(days=2)


class FileSpendLedger:
    """Durable ledger in one JSONL file, shared by every process on the host that uses it.

    A restart no longer resets the counters: each record is appended under an exclusive
    `flock` and fsynced before the call it pays for is made. Readers take a shared lock.
    Lines that cannot be parsed are not skipped: an unreadable ledger fails closed, because
    a breaker that under-counts is worse than one that stops work.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)

    def spent(self, since: datetime, domain: Domain | None = None) -> float:
        with self._locked(fcntl.LOCK_SH) as fh:
            return sum(
                eur
                for at, d, eur in _parse_lines(fh.read(), self._path)
                if at >= since and (domain is None or d is domain)
            )

    def record(self, at: datetime, domain: Domain, eur: float) -> None:
        line = json.dumps(
            {"at": at.astimezone(UTC).isoformat(), "domain": domain.value, "eur": eur}
        )
        with self._locked(fcntl.LOCK_EX) as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def compact(self, now: datetime) -> int:
        """Drop entries older than RETENTION. Returns how many were kept."""
        with self._locked(fcntl.LOCK_EX) as fh:
            kept = [
                (at, d, eur)
                for at, d, eur in _parse_lines(fh.read(), self._path)
                if at >= now - RETENTION
            ]
            tmp = self._path.with_name(f".{self._path.name}.{uuid4().hex}")
            with tmp.open("w", encoding="utf-8") as out:
                for at, d, eur in kept:
                    out.write(json.dumps({"at": at.isoformat(), "domain": d.value, "eur": eur}))
                    out.write("\n")
                out.flush()
                os.fsync(out.fileno())
            tmp.chmod(0o600)
            os.replace(tmp, self._path)
        return len(kept)

    @contextmanager
    def _locked(self, mode: int) -> Iterator[TextIO]:
        with self._path.open("a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), mode)
            try:
                fh.seek(0)
                yield fh
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class LedgerCorrupt(BudgetExceeded):
    """The ledger cannot be read; the breaker treats that as no budget left."""


def _parse_lines(text: str, path: Path) -> Iterator[tuple[datetime, Domain, float]]:
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            at = datetime.fromisoformat(item["at"])
            eur = float(item["eur"])
            domain = Domain(item["domain"])
        except (ValueError, KeyError, TypeError) as exc:
            raise LedgerCorrupt(f"spend ledger {path} unreadable at line {number}") from exc
        if at.tzinfo is None or eur < 0:
            raise LedgerCorrupt(f"spend ledger {path} unreadable at line {number}")
        yield at, domain, eur


class RedisCommands(Protocol):
    """The part of a redis-py client the ledger uses (decode_responses may be on or off)."""

    def zadd(self, name: str, mapping: dict[str, float]) -> object: ...

    def zrangebyscore(self, name: str, min: float, max: str) -> list[bytes] | list[str]: ...

    def zremrangebyscore(self, name: str, min: str, max: float) -> object: ...


class RedisSpendLedger:
    """Production ledger in redis-core: one sorted set per domain, scored by epoch seconds.

    Member names carry the amount and a random id, so equal amounts at the same instant never
    collapse into one member. Entries older than RETENTION are trimmed on every write.
    """

    def __init__(self, client: RedisCommands, prefix: str = "jarvis:finops:spend") -> None:
        self._redis = client
        self._prefix = prefix

    def _key(self, domain: Domain) -> str:
        return f"{self._prefix}:{domain.value}"

    def spent(self, since: datetime, domain: Domain | None = None) -> float:
        domains = [domain] if domain is not None else list(Domain)
        total = 0.0
        for d in domains:
            for member in self._redis.zrangebyscore(self._key(d), since.timestamp(), "+inf"):
                text = member.decode() if isinstance(member, bytes) else member
                try:
                    total += float(text.split(":", 1)[0])
                except ValueError as exc:
                    raise LedgerCorrupt(f"spend ledger member {text!r} unreadable") from exc
        return total

    def record(self, at: datetime, domain: Domain, eur: float) -> None:
        key = self._key(domain)
        self._redis.zadd(key, {f"{eur!r}:{uuid4().hex}": at.timestamp()})
        self._redis.zremrangebyscore(key, "-inf", (at - RETENTION).timestamp())


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
