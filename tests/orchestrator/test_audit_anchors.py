"""Domain audit chains anchored in core's log: a truncated or rewritten chain is caught."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from jarvis_core.audit.chain import GENESIS_HASH, AuditWriter
from jarvis_core.audit.file import FileAuditStore
from jarvis_core.boundary.anchor import anchor_event, matches_anchor
from jarvis_core.boundary.mailbox import Mailbox
from jarvis_core.contracts.audit import AuditAction, AuditDecision, AuditLogEntry
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import AuditAnchor
from jarvis_core.orchestrator.anchors import AnchorBook, AnchorVerdict
from jarvis_core.orchestrator.service import Orchestrator
from jarvis_core.policy.flows import check_flow
from mod_content.reel import __main__ as content_cli

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def entry() -> AuditLogEntry:
    return AuditLogEntry(
        trace_id=uuid4().hex,
        domain=Domain.CONTENT,
        actor="spiffe://jarvis.home/content/reel-worker",
        action=AuditAction.TOOL_CALL,
        target="sandbox.fetch",
        decision=AuditDecision.ALLOW,
    )


def chain(path: Path, n: int) -> FileAuditStore:
    store = FileAuditStore(path)
    writer = AuditWriter(store)
    for _ in range(n):
        writer.write(entry())
    return store


def anchor(domain: Domain, entries: int, head: str) -> AuditAnchor:
    return AuditAnchor(
        origin_domain=domain,
        target_domain=Domain.CORE,
        classification=DataClassification.INTERNAL,
        chain="reel",
        entries=entries,
        head_hash=head,
    )


def test_every_domain_may_anchor_in_core_but_nowhere_else() -> None:
    for domain in (Domain.TRADING, Domain.CONTENT, Domain.PRIVATE):
        assert check_flow(anchor(domain, 1, "a" * 64), caller_domain=domain).allowed
    to_trading = anchor(Domain.CONTENT, 1, "a" * 64).model_copy(
        update={"target_domain": Domain.TRADING}
    )
    assert not check_flow(to_trading, caller_domain=Domain.CONTENT).allowed


def test_matches_anchor_catches_truncation_and_rewrite(tmp_path: Path) -> None:
    store = chain(tmp_path / "audit.jsonl", 3)
    event = anchor_event(Domain.CONTENT, "reel", store.entries())
    assert event.entries == 3 and event.head_hash == store.head()
    chain(tmp_path / "audit.jsonl", 2)  # appending later keeps old anchors valid
    entries = FileAuditStore(tmp_path / "audit.jsonl").entries()
    assert matches_anchor(entries, 3, event.head_hash)
    assert not matches_anchor(entries[:2], 3, event.head_hash)  # truncated
    rewritten = chain(tmp_path / "other.jsonl", 5).entries()  # same length, other history
    assert not matches_anchor(rewritten, 3, event.head_hash)
    assert matches_anchor([], 0, GENESIS_HASH)


def test_anchor_book_verdicts_and_persistence(tmp_path: Path) -> None:
    book = AnchorBook(tmp_path / "anchors.json")
    assert book.record(Domain.CONTENT, anchor(Domain.CONTENT, 3, "a" * 64)) is AnchorVerdict.FIRST
    assert book.record(Domain.CONTENT, anchor(Domain.CONTENT, 3, "a" * 64)) is (
        AnchorVerdict.UNCHANGED
    )
    assert book.record(Domain.CONTENT, anchor(Domain.CONTENT, 5, "b" * 64)) is (
        AnchorVerdict.ADVANCED
    )
    reopened = AnchorBook(tmp_path / "anchors.json")
    assert reopened.record(Domain.CONTENT, anchor(Domain.CONTENT, 4, "c" * 64)) is (
        AnchorVerdict.ROLLED_BACK
    )
    assert reopened.record(Domain.CONTENT, anchor(Domain.CONTENT, 5, "d" * 64)) is (
        AnchorVerdict.ROLLED_BACK
    )
    # The last good head stays the reference.
    assert [a.entries for a in reopened.latest()] == [5]
    # Chains are kept apart per domain.
    assert reopened.record(Domain.TRADING, anchor(Domain.TRADING, 1, "e" * 64)) is (
        AnchorVerdict.FIRST
    )


def test_orchestrator_writes_anchors_into_cores_log_and_denies_rollbacks(tmp_path: Path) -> None:
    audit: list[AuditLogEntry] = []
    orchestrator = Orchestrator(
        Mailbox(tmp_path / "boundary", Domain.CORE),
        None,
        clock=lambda: NOW,
        audit=audit.append,
        anchors=AnchorBook(tmp_path / "anchors.json"),
    )
    content = Mailbox(tmp_path / "boundary", Domain.CONTENT)
    content.send(anchor(Domain.CONTENT, 3, "a" * 64))
    content.send(anchor(Domain.CONTENT, 3, "a" * 64))
    orchestrator.sync()
    content.send(anchor(Domain.CONTENT, 1, "b" * 64))
    orchestrator.sync()

    assert [(e.target, e.decision, e.content_hash, e.content_ref) for e in audit] == [
        ("audit_anchor:content/reel", AuditDecision.ALLOW, "a" * 64, "entries=3"),
        ("audit_anchor:content/reel", AuditDecision.DENY, "b" * 64, "entries=1"),
    ]
    assert audit[1].policy_id == "audit-anchor-rollback"
    assert content.receive(Domain.CORE) == []
    assert Mailbox(tmp_path / "boundary", Domain.CORE).receive(Domain.CONTENT) == []


def test_reel_worker_anchors_its_chain_after_each_reel(tmp_path: Path) -> None:
    data = tmp_path / "content"
    data.mkdir()
    store = chain(data / "audit.jsonl", 4)
    content = Mailbox(tmp_path / "boundary", Domain.CONTENT)

    content_cli._anchor(content, data)

    [letter] = Mailbox(tmp_path / "boundary", Domain.CORE).receive(Domain.CONTENT)
    assert isinstance(letter.event, AuditAnchor)
    assert (letter.event.entries, letter.event.head_hash) == (4, store.head())


def test_reel_worker_stops_on_a_broken_local_chain(tmp_path: Path) -> None:
    data = tmp_path / "content"
    data.mkdir()
    chain(data / "audit.jsonl", 2)
    lines = (data / "audit.jsonl").read_text().splitlines()
    (data / "audit.jsonl").write_text(lines[1] + "\n")  # first entry deleted
    with pytest.raises(Exception, match="broken"):
        content_cli._anchor(Mailbox(tmp_path / "boundary", Domain.CONTENT), data)
