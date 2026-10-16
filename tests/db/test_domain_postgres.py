"""Per-domain database roles: agents read curated views only."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import conninfo

pytestmark = pytest.mark.db

DSN = os.environ.get("JARVIS_TEST_PG_DSN")
SQL = Path(__file__).parents[2] / "docker/postgres/domain/sql/domain.sql"
DB = "jarvis_domain_test"
PASSWORD = "test-only-password"  # noqa: S105


def _dsn(**overrides: str) -> str:
    assert DSN is not None
    params = conninfo.conninfo_to_dict(DSN)
    params.update(overrides)
    return conninfo.make_conninfo(**params)  # type: ignore[arg-type]


def _drop(admin: psycopg.Connection[Any]) -> None:
    admin.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
    admin.execute("DROP ROLE IF EXISTS app")
    admin.execute("DROP ROLE IF EXISTS agent_ro")


@pytest.fixture(scope="module")
def domain_db() -> Iterator[None]:
    if not DSN:
        pytest.skip("JARVIS_TEST_PG_DSN not set")
    with psycopg.connect(DSN, autocommit=True) as admin:
        _drop(admin)
        admin.execute(f"CREATE DATABASE {DB}")
    with psycopg.connect(_dsn(dbname=DB), autocommit=True) as conn:
        conn.execute(SQL.read_text())
        conn.execute(f"ALTER ROLE app LOGIN PASSWORD '{PASSWORD}'")
        conn.execute(f"ALTER ROLE agent_ro LOGIN PASSWORD '{PASSWORD}'")
    with psycopg.connect(_dsn(dbname=DB, user="app", password=PASSWORD), autocommit=True) as app:
        app.execute("CREATE TABLE app.positions (symbol text, qty numeric)")
        app.execute("CREATE VIEW agent_views.symbols AS SELECT DISTINCT symbol FROM app.positions")
    yield
    with psycopg.connect(DSN, autocommit=True) as admin:
        _drop(admin)


def _agent() -> psycopg.Connection[Any]:
    return psycopg.connect(_dsn(dbname=DB, user="agent_ro", password=PASSWORD), autocommit=True)


def test_agent_reads_curated_views(domain_db: None) -> None:
    with _agent() as conn:
        assert conn.execute("SELECT count(*) FROM agent_views.symbols").fetchone() == (0,)


def test_agent_cannot_read_raw_tables(domain_db: None) -> None:
    with _agent() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("SELECT * FROM app.positions")


def test_agent_cannot_create_objects(domain_db: None) -> None:
    with _agent() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("CREATE TABLE public.x (id int)")
