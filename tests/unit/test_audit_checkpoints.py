"""Signed checkpoints, WORM bundles and their offline verification."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jarvis_core.audit.__main__ import main
from jarvis_core.audit.chain import AuditWriter, ChainedEntry, InMemoryAuditStore
from jarvis_core.audit.checkpoint import (
    Checkpoint,
    CheckpointError,
    InMemoryCheckpointStore,
    key_id,
    load_private_key,
    next_checkpoint,
    public_key_hex,
    sign,
    verify_log,
)
from jarvis_core.audit.export import export_bundle, exported_upto, verify_bundles
from jarvis_core.audit.file import FileAuditStore
from jarvis_core.contracts.audit import AuditAction, AuditDecision, AuditLogEntry
from jarvis_core.contracts.classification import Domain

SEED = "11" * 32
OTHER = "22" * 32
T0 = datetime(2026, 10, 16, 8, 0, tzinfo=UTC)
KEY = load_private_key(SEED)
KEYS = {key_id(KEY.public_key()): KEY.public_key()}


def entry(n: int) -> AuditLogEntry:
    return AuditLogEntry(
        trace_id=f"t{n}",
        domain=Domain.CORE,
        actor="test",
        action=AuditAction.TOOL_CALL,
        target=f"tool:{n}",
        decision=AuditDecision.ALLOW,
    )


def log(n: int) -> tuple[InMemoryAuditStore, AuditWriter]:
    store = InMemoryAuditStore()
    writer = AuditWriter(store)
    for i in range(n):
        writer.write(entry(i))
    return store, writer


def test_signed_head_verifies() -> None:
    store, _ = log(3)
    checkpoints = InMemoryCheckpointStore()
    cp = next_checkpoint(3, store.head(), checkpoints, KEY, T0)
    assert cp.upto_seq == 3 and cp.key_id == key_id(KEY.public_key())
    assert verify_log(store.entries, checkpoints.all(), KEYS) == []


def test_signature_survives_a_json_round_trip() -> None:
    cp = sign(KEY, 0, "0" * 64, T0 + timedelta(microseconds=7))
    again = Checkpoint.model_validate_json(cp.model_dump_json())
    assert verify_log([], [again], KEYS) == []


def test_rewritten_history_breaks_the_signature() -> None:
    store, _ = log(3)
    checkpoints = InMemoryCheckpointStore()
    next_checkpoint(3, store.head(), checkpoints, KEY, T0)
    # An attacker with write access rebuilds the chain with a changed entry 2.
    forged_store, forged = InMemoryAuditStore(), None
    forged = AuditWriter(forged_store)
    for i in range(3):
        forged.write(entry(i if i != 1 else 99))
    findings = verify_log(forged_store.entries, checkpoints.all(), KEYS)
    assert [f.kind for f in findings] == ["head"]


def test_wrong_key_and_tampered_fields_are_found() -> None:
    store, _ = log(2)
    other = load_private_key(OTHER)
    foreign = sign(other, 2, store.head(), T0)
    assert [f.kind for f in verify_log(store.entries, [foreign], KEYS)] == ["signature"]
    good = sign(KEY, 2, store.head(), T0)
    moved = good.model_copy(update={"signed_at": T0 + timedelta(hours=1)})
    assert "signature" in {f.kind for f in verify_log(store.entries, [moved], KEYS)}


def test_truncated_log_is_found() -> None:
    store, _ = log(4)
    cp = sign(KEY, 4, store.head(), T0)
    findings = verify_log(store.entries[:2], [cp], KEYS)
    assert {f.kind for f in findings} == {"missing"}


def test_checkpoints_never_go_backwards() -> None:
    store, writer = log(2)
    checkpoints = InMemoryCheckpointStore()
    next_checkpoint(2, store.head(), checkpoints, KEY, T0)
    with pytest.raises(CheckpointError):
        next_checkpoint(1, store.entries[0].entry_hash, checkpoints, KEY, T0 + timedelta(hours=1))
    with pytest.raises(CheckpointError):
        next_checkpoint(2, store.head(), checkpoints, KEY, T0)
    with pytest.raises(CheckpointError):
        next_checkpoint(2, "a" * 64, checkpoints, KEY, T0 + timedelta(hours=1))
    # An hour without new entries still gets a checkpoint (liveness).
    next_checkpoint(2, store.head(), checkpoints, KEY, T0 + timedelta(hours=1))
    writer.write(entry(3))
    next_checkpoint(3, store.head(), checkpoints, KEY, T0 + timedelta(hours=2))
    assert verify_log(store.entries, checkpoints.all(), KEYS) == []


def test_bad_seed_is_refused() -> None:
    with pytest.raises(CheckpointError):
        load_private_key("abcd")


# --- WORM bundles ---------------------------------------------------------------------------------


def exported(tmp_path: Path) -> tuple[InMemoryAuditStore, AuditWriter, InMemoryCheckpointStore]:
    store, writer = log(3)
    checkpoints = InMemoryCheckpointStore()
    next_checkpoint(3, store.head(), checkpoints, KEY, T0)
    assert export_bundle(store.entries, checkpoints.all(), tmp_path) is not None
    return store, writer, checkpoints


def test_bundles_continue_and_verify(tmp_path: Path) -> None:
    store, writer, checkpoints = exported(tmp_path)
    writer.write(entry(3))
    writer.write(entry(4))
    # Entries after the last checkpoint wait for the next one.
    assert export_bundle(store.entries, checkpoints.all(), tmp_path) is None
    next_checkpoint(5, store.head(), checkpoints, KEY, T0 + timedelta(hours=1))
    second = export_bundle(store.entries, checkpoints.all(), tmp_path)
    assert second is not None and (second.first_seq, second.last_seq) == (4, 5)
    assert exported_upto(tmp_path) == 5
    assert verify_bundles(tmp_path, KEYS) == []


def test_bundle_files_are_read_only_and_never_overwritten(tmp_path: Path) -> None:
    exported(tmp_path)
    files = sorted(tmp_path.iterdir())
    assert len(files) == 2
    for file in files:
        assert stat.S_IMODE(file.stat().st_mode) == 0o444
    with pytest.raises(FileExistsError):
        fd = os.open(files[0], os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        os.close(fd)


def test_edited_bundle_is_found(tmp_path: Path) -> None:
    exported(tmp_path)
    body = next(tmp_path.glob("*.jsonl"))
    body.chmod(0o644)
    lines = body.read_text().splitlines()
    item = json.loads(lines[1])
    item["payload"] = item["payload"].replace("tool:1", "tool:7")
    lines[1] = json.dumps(item)
    body.write_text("\n".join(lines) + "\n")
    problems = verify_bundles(tmp_path, KEYS)
    assert any("entries file was changed" in p for p in problems)
    assert any("hash does not match" in p for p in problems)


def test_missing_bundle_breaks_continuity(tmp_path: Path) -> None:
    store, writer, checkpoints = exported(tmp_path)
    writer.write(entry(3))
    next_checkpoint(4, store.head(), checkpoints, KEY, T0 + timedelta(hours=1))
    export_bundle(store.entries, checkpoints.all(), tmp_path)
    for file in tmp_path.glob("audit-000000000001-*"):
        file.unlink()
    assert any("does not continue" in p for p in verify_bundles(tmp_path, KEYS))


def test_export_refuses_a_log_that_disagrees_with_its_checkpoint(tmp_path: Path) -> None:
    store, _ = log(2)
    cp = sign(KEY, 2, "b" * 64, T0)
    from jarvis_core.audit.export import ExportError

    with pytest.raises(ExportError):
        export_bundle(store.entries, [cp], tmp_path)


# --- command line, with a domain's file chain -----------------------------------------------------


def test_cli_on_a_file_chain(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    secrets = tmp_path / "secrets" / "trading"
    secrets.mkdir(parents=True)
    (secrets / "audit_signing_key").write_text(SEED)
    chain = tmp_path / "audit.jsonl"
    writer = AuditWriter(FileAuditStore(chain))
    for i in range(3):
        writer.write(entry(i))
    base = ["--domain", "trading", "--secrets-root", str(tmp_path / "secrets")]
    assert main([*base, "public-key"]) == 0
    public = capsys.readouterr().out.strip()
    assert public == public_key_hex(KEY.public_key())
    assert main([*base, "checkpoint", "--file", str(chain)]) == 0
    assert (tmp_path / "audit.checkpoints.jsonl").exists()
    worm = tmp_path / "worm"
    assert main([*base, "export", "--file", str(chain), "--out", str(worm)]) == 0
    assert main([*base, "verify", "--file", str(chain), "--public-key", public]) == 0
    assert main([*base, "verify", "--bundles", str(worm), "--public-key", public]) == 0
    assert main([*base, "verify", "--bundles", str(worm), "--public-key", "33" * 32]) == 1


def test_chained_entry_positions_match_export(tmp_path: Path) -> None:
    store, _, _ = exported(tmp_path)
    lines = next(tmp_path.glob("*.jsonl")).read_text().splitlines()
    assert [json.loads(line)["seq"] for line in lines] == [1, 2, 3]
    assert [json.loads(line)["hash"] for line in lines] == [e.entry_hash for e in store.entries]
    assert isinstance(store.entries[0], ChainedEntry)
