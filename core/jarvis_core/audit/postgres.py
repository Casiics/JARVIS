"""Postgres-backed audit store. The writer role may only call audit.head() and audit.append()."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jarvis_core.audit.chain import AuditChainError, ChainedEntry
from jarvis_core.audit.checkpoint import Checkpoint, CheckpointError

if TYPE_CHECKING:
    import psycopg


class PostgresAuditStore:
    """Writer side: head() and append(). Reader side: tip() and entries()."""

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

    def tip(self) -> tuple[int, str]:
        row: Any = self._conn.execute("SELECT n, head FROM audit.tip()").fetchone()
        if row is None:
            raise AuditChainError("audit.tip() returned no row")
        return int(row[0]), str(row[1])

    def entries(self) -> list[ChainedEntry]:
        """The whole log in order. Needs the audit_reader role."""
        rows: list[Any] = self._conn.execute(
            "SELECT payload, prev_hash, entry_hash FROM audit.log ORDER BY seq"
        ).fetchall()
        return [ChainedEntry(str(p), str(prev), str(h)) for p, prev, h in rows]


class PostgresCheckpointStore:
    def __init__(self, conn: psycopg.Connection[tuple[str]]) -> None:
        self._conn = conn

    def last(self) -> Checkpoint | None:
        row = self._conn.execute("SELECT audit.last_checkpoint()").fetchone()
        if row is None or row[0] is None:
            return None
        return Checkpoint.model_validate_json(row[0])

    def append(self, checkpoint: Checkpoint) -> None:
        import psycopg

        try:
            self._conn.execute(
                "SELECT audit.checkpoint_append(%s)", (checkpoint.model_dump_json(),)
            )
            self._conn.commit()
        except psycopg.errors.RaiseException as exc:
            self._conn.rollback()
            raise CheckpointError(str(exc)) from exc

    def all(self) -> list[Checkpoint]:
        """Needs the audit_reader role."""
        rows = self._conn.execute("SELECT body FROM audit.checkpoint ORDER BY id").fetchall()
        return [Checkpoint.model_validate_json(row[0]) for row in rows]
