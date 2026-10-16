"""The API gateway never drops audit entries silently: without pg-audit it keeps a file chain."""

from __future__ import annotations

import argparse
from pathlib import Path
from uuid import uuid4

import pytest

from jarvis_core.api.__main__ import _audit_store
from jarvis_core.audit.chain import AuditWriter, InMemoryAuditStore
from jarvis_core.audit.file import FileAuditStore
from jarvis_core.contracts.audit import AuditAction, AuditDecision, AuditLogEntry
from jarvis_core.contracts.classification import Domain


def _args(tmp_path: Path, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "audit_dsn": None,
        "audit_file": None,
        "audit_memory": False,
        "origin": "https://jarvis.tailnet.ts.net",
        "data_dir": tmp_path / "deck",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _entry() -> AuditLogEntry:
    return AuditLogEntry(
        trace_id=uuid4().hex,
        domain=Domain.CORE,
        actor="spiffe://jarvis.home/core/api-gateway",
        action=AuditAction.STATE_TRANSITION,
        target="deck",
        decision=AuditDecision.ALLOW,
    )


def test_default_is_a_durable_chain_in_the_data_dir(tmp_path: Path) -> None:
    store = _audit_store(_args(tmp_path))
    assert isinstance(store, FileAuditStore)
    AuditWriter(store).write(_entry())
    reopened = FileAuditStore(tmp_path / "deck" / "audit.jsonl")
    assert len(reopened.entries()) == 1
    assert reopened.head() == store.head()


def test_an_explicit_audit_file_is_used(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere" / "chain.jsonl"
    store = _audit_store(_args(tmp_path, audit_file=target))
    AuditWriter(store).write(_entry())
    assert target.exists()


def test_memory_only_on_request_and_never_behind_https(tmp_path: Path) -> None:
    local = _audit_store(_args(tmp_path, audit_memory=True, origin="http://localhost:8080"))
    assert isinstance(local, InMemoryAuditStore)
    with pytest.raises(SystemExit, match="audit-dsn"):
        _audit_store(_args(tmp_path, audit_memory=True))
