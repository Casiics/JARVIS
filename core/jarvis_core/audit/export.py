"""Daily WORM export: sealed, signed bundles of the audit log for write-once storage.

Each bundle covers the entries after the previous bundle up to the newest checkpoint, so every
bundle ends on a signed head:

    audit-<from>-<to>.jsonl           one line per entry: seq, payload, prev, hash
    audit-<from>-<to>.manifest.json   range, SHA-256 of the .jsonl, the covering checkpoint

Files are created exclusively (never overwritten) and made read-only. The directory is meant
to be synced to storage that refuses deletion and overwriting, such as an S3 bucket with
Object Lock in compliance mode (docs/runbooks/audit-hardening.md). `verify_bundles` checks a
directory of bundles offline with nothing but the public key.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from jarvis_core.audit.chain import GENESIS_HASH, ChainedEntry, compute_hash
from jarvis_core.audit.checkpoint import Checkpoint, check_signature

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

FORMAT = "jarvis-audit-bundle/v1"


class ExportError(Exception):
    pass


class Manifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format: str = FORMAT
    first_seq: int = Field(ge=1)
    last_seq: int = Field(ge=1)
    prev_hash: str = Field(pattern=r"^[0-9a-f]{64}$", description="hash before first_seq")
    head_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint: Checkpoint


def _name(first: int, last: int) -> str:
    return f"audit-{first:012d}-{last:012d}"


def _write_once(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def exported_upto(directory: Path) -> int:
    last = 0
    for manifest in directory.glob("audit-*.manifest.json"):
        last = max(last, Manifest.model_validate_json(manifest.read_text("utf-8")).last_seq)
    return last


def export_bundle(
    entries: Sequence[ChainedEntry],
    checkpoints: Sequence[Checkpoint],
    directory: Path,
) -> Manifest | None:
    """Write the next bundle, or return None when no checkpoint covers new entries.

    `entries` is the whole log in order (entry n has seq n, starting at 1).
    """
    directory.mkdir(parents=True, exist_ok=True)
    start = exported_upto(directory) + 1
    covering = [c for c in checkpoints if c.upto_seq >= start]
    if not covering:
        return None
    checkpoint = max(covering, key=lambda c: (c.upto_seq, c.signed_at))
    if checkpoint.upto_seq > len(entries):
        raise ExportError("checkpoint is ahead of the log")
    batch = entries[start - 1 : checkpoint.upto_seq]
    if batch[-1].entry_hash != checkpoint.head_hash:
        raise ExportError("checkpoint head does not match the log; refusing to export")
    lines = [
        json.dumps(
            {"seq": start + i, "payload": e.payload, "prev": e.prev_hash, "hash": e.entry_hash},
            sort_keys=True,
            separators=(",", ":"),
        )
        for i, e in enumerate(batch)
    ]
    body = ("\n".join(lines) + "\n").encode()
    manifest = Manifest(
        first_seq=start,
        last_seq=checkpoint.upto_seq,
        prev_hash=batch[0].prev_hash,
        head_hash=checkpoint.head_hash,
        entries_sha256=hashlib.sha256(body).hexdigest(),
        checkpoint=checkpoint,
    )
    name = _name(start, checkpoint.upto_seq)
    _write_once(directory / f"{name}.jsonl", body)
    _write_once(directory / f"{name}.manifest.json", manifest.model_dump_json(indent=2).encode())
    return manifest


def verify_bundles(directory: Path, keys: Mapping[str, Ed25519PublicKey]) -> list[str]:
    """Check every bundle in order: file hash, chain links, continuity, signed head."""
    problems: list[str] = []
    manifests = sorted(
        (
            Manifest.model_validate_json(p.read_text("utf-8"))
            for p in directory.glob("audit-*.manifest.json")
        ),
        key=lambda m: m.first_seq,
    )
    expected_seq, expected_prev = 1, GENESIS_HASH
    for manifest in manifests:
        name = _name(manifest.first_seq, manifest.last_seq)
        if manifest.first_seq != expected_seq or manifest.prev_hash != expected_prev:
            problems.append(f"{name}: does not continue the previous bundle")
        body = (directory / f"{name}.jsonl").read_bytes()
        if hashlib.sha256(body).hexdigest() != manifest.entries_sha256:
            problems.append(f"{name}: entries file was changed")
        prev = manifest.prev_hash
        seq = manifest.first_seq
        for line in body.decode().splitlines():
            item = json.loads(line)
            if item["seq"] != seq or item["prev"] != prev:
                problems.append(f"{name}: entry {item['seq']} is out of sequence")
                break
            if item["hash"] != compute_hash(prev, item["payload"]):
                problems.append(f"{name}: entry {seq} hash does not match its payload")
                break
            prev, seq = item["hash"], seq + 1
        if seq - 1 != manifest.last_seq or prev != manifest.head_hash:
            problems.append(f"{name}: entries do not end at the signed head")
        cp = manifest.checkpoint
        if cp.upto_seq != manifest.last_seq or cp.head_hash != manifest.head_hash:
            problems.append(f"{name}: checkpoint does not cover this bundle")
        if not check_signature(cp, keys):
            problems.append(f"{name}: checkpoint signature is invalid or from an unknown key")
        expected_seq, expected_prev = manifest.last_seq + 1, manifest.head_hash
    return problems
