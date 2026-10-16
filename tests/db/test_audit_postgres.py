"""Audit log guarantees enforced by the database itself.

Needs JARVIS_TEST_PG_DSN pointing at a disposable Postgres server as a superuser,
e.g. postgresql://postgres:postgres@localhost:5432/postgres
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import conninfo

from jarvis_core.audit.chain import AuditChainError, AuditWriter, ChainedEntry, verify_chain
from jarvis_core.audit.postgres import PostgresAuditStore
from jarvis_core.contracts.audit import AuditAction, AuditDecision, AuditLogEntry
from jarvis_core.contracts.classification import Domain

pytestmark = pytest.mark.db

DSN = os.environ.get("JARVIS_TEST_PG_DSN")
SQL = Path(__file__).parents[2] / "docker/postgres/audit/sql/audit.sql"
DB = "jarvis_audit_test"
PASSWORD = "test-only-password"  # noqa: S105


def _dsn(**overrides: str) -> str:
    assert DSN is not None
    params = conninfo.conninfo_to_dict(DSN)
    params.update(overrides)
    return conninfo.make_conninfo(**params)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def audit_db() -> Iterator[None]:
    if not DSN:
        pytest.skip("JARVIS_TEST_PG_DSN not set")
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        admin.execute("DROP ROLE IF EXISTS audit_writer")
        admin.execute("DROP ROLE IF EXISTS audit_reader")
        admin.execute(f"CREATE DATABASE {DB}")
    with psycopg.connect(_dsn(dbname=DB), autocommit=True) as conn:
        conn.execute(SQL.read_text())
        conn.execute(f"ALTER ROLE audit_writer LOGIN PASSWORD '{PASSWORD}'")
        conn.execute(f"ALTER ROLE audit_reader LOGIN PASSWORD '{PASSWORD}'")
    yield
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        admin.execute("DROP ROLE IF EXISTS audit_writer")
        admin.execute("DROP ROLE IF EXISTS audit_reader")


def _connect(role: str) -> psycopg.Connection[Any]:
    return psycopg.connect(_dsn(dbname=DB, user=role, password=PASSWORD))


def _entry(n: int) -> AuditLogEntry:
    return AuditLogEntry(
        trace_id=f"t{n}",
        domain=Domain.TRADING,
        actor="spiffe://jarvis.home/trading/worker",
        action=AuditAction.GATE_DECISION,
        target="risk_gate",
        decision=AuditDecision.DENY,
    )


def test_writer_appends_a_valid_chain(audit_db: None) -> None:
    with _connect("audit_writer") as conn:
        writer = AuditWriter(PostgresAuditStore(conn))
        for n in range(3):
            writer.write(_entry(n))
    with _connect("audit_reader") as conn:
        rows = conn.execute("SELECT payload, prev_hash, entry_hash FROM audit.log ORDER BY seq")
        entries = [ChainedEntry(p, h0, h1) for p, h0, h1 in rows.fetchall()]
    assert len(entries) >= 3
    assert verify_chain(entries) is None


def test_writer_cannot_forge_a_link(audit_db: None) -> None:
    with _connect("audit_writer") as conn:
        store = PostgresAuditStore(conn)
        with pytest.raises(AuditChainError):
            store.append(ChainedEntry("{}", store.head(), "0" * 64))


def test_writer_has_no_direct_table_access(audit_db: None) -> None:
    with _connect("audit_writer") as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("SELECT * FROM audit.log")


def test_reader_cannot_write(audit_db: None) -> None:
    with _connect("audit_reader") as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("SELECT audit.append('{}', repeat('0', 64), repeat('0', 64))")


def test_even_the_owner_cannot_update_or_delete(audit_db: None) -> None:
    with psycopg.connect(_dsn(dbname=DB), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("UPDATE audit.log SET payload = '{}'")
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("DELETE FROM audit.log")
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("TRUNCATE audit.log")
