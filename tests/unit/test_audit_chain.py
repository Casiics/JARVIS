from dataclasses import replace

import pytest

from jarvis_core.audit.chain import (
    GENESIS_HASH,
    AuditChainError,
    AuditWriter,
    InMemoryAuditStore,
    compute_hash,
    verify_chain,
)
from jarvis_core.contracts.audit import AuditAction, AuditDecision, AuditLogEntry
from jarvis_core.contracts.classification import Domain


def _entry(n: int) -> AuditLogEntry:
    return AuditLogEntry(
        trace_id=f"t{n}",
        domain=Domain.CORE,
        actor="spiffe://jarvis.home/core/test",
        action=AuditAction.TOOL_CALL,
        target=f"tool-{n}",
        decision=AuditDecision.ALLOW,
    )


def test_chain_links_and_verifies() -> None:
    store = InMemoryAuditStore()
    writer = AuditWriter(store)
    for n in range(5):
        writer.write(_entry(n))
    assert store.entries[0].prev_hash == GENESIS_HASH
    assert verify_chain(store.entries) is None


def test_tampering_is_detected() -> None:
    store = InMemoryAuditStore()
    writer = AuditWriter(store)
    for n in range(3):
        writer.write(_entry(n))
    store.entries[1] = replace(store.entries[1], payload=store.entries[1].payload.replace("1", "9"))
    assert verify_chain(store.entries) == 1


def test_store_rejects_forged_link() -> None:
    store = InMemoryAuditStore()
    with pytest.raises(AuditChainError):
        from jarvis_core.audit.chain import ChainedEntry

        store.append(ChainedEntry("{}", "f" * 64, compute_hash("f" * 64, "{}")))
