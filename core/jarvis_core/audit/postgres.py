"""Postgres-backed audit store. The writer role may only call audit.head() and audit.append()."""

from __future__ import annotations

from typing import TYPE_CHECKING

from jarvis_core.audit.chain import AuditChainError, ChainedEntry

if TYPE_CHECKING:
    import psycopg


class PostgresAuditStore:
    def __init__(self, conn: psycopg.Connection[tuple[str]]) -> None:
        self._conn = conn

    def head(self) -> str:
        row = self._conn.execute("SELECT audit.head()").fetchone()
        if row is None:
            raise AuditChainError("audit.head() returned no row")
        return row[0]

    def append(self, chained: ChainedEntry) -> None:
        import psycopg

        try:
            self._conn.execute(
                "SELECT audit.append(%s, %s, %s)",
                (chained.payload, chained.prev_hash, chained.entry_hash),
            )
            self._conn.commit()
        except psycopg.errors.RaiseException as exc:
            self._conn.rollback()
            raise AuditChainError(str(exc)) from exc
