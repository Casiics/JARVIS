"""Audit chain upkeep: hourly checkpoints, daily WORM export, offline verification.

    # pg-audit (core): sign the head, then export new signed entries
    python -m jarvis_core.audit checkpoint \\
        --dsn "host=pg-audit dbname=postgres user=audit_writer" \\
        --password-file /run/secrets/pg_audit_writer_password
    python -m jarvis_core.audit export --dsn "... user=audit_reader" --password-file ... \\
        --out /worm
    # A domain's file chain (private, trading): same commands with --file
    python -m jarvis_core.audit --domain trading checkpoint \\
        --file /var/lib/jarvis/trading/audit.jsonl
    # Anywhere, with only the public key:
    python -m jarvis_core.audit verify --bundles /worm --public-key <hex>
    python -m jarvis_core.audit public-key --domain core

The signing key is the domain's `audit_signing_key` secret (32-byte Ed25519 seed, hex), read
through the SecretProvider. Store the printed public key somewhere off the server.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from jarvis_core.audit.chain import ChainedEntry
from jarvis_core.audit.checkpoint import (
    CheckpointError,
    CheckpointStore,
    key_id,
    load_private_key,
    load_public_key,
    next_checkpoint,
    public_key_hex,
    verify_log,
)
from jarvis_core.audit.export import export_bundle, verify_bundles
from jarvis_core.audit.file import FileAuditStore, FileCheckpointStore
from jarvis_core.contracts.classification import Domain
from jarvis_core.secrets.provider import UnpackedSopsProvider


def _checkpoints_path(chain: Path) -> Path:
    return chain.with_name(chain.stem + ".checkpoints.jsonl")


def _open(args: argparse.Namespace) -> tuple[list[ChainedEntry], CheckpointStore, int, str]:
    """Entries, checkpoint store, entry count and head, from a file chain or pg-audit."""
    if args.file is not None:
        chain = FileAuditStore(args.file)
        entries = chain.entries()
        return (
            entries,
            FileCheckpointStore(_checkpoints_path(args.file)),
            len(entries),
            chain.head(),
        )
    if args.dsn is None:
        raise SystemExit("either --file or --dsn is required")
    import psycopg

    from jarvis_core.audit.postgres import PostgresAuditStore, PostgresCheckpointStore

    password = args.password_file.read_text().strip() if args.password_file else None
    conn = psycopg.connect(args.dsn, password=password) if password else psycopg.connect(args.dsn)
    store = PostgresAuditStore(conn)
    count, head = store.tip()
    entries = store.entries() if args.command != "checkpoint" else []
    return entries, PostgresCheckpointStore(conn), count, head


def _keys(hex_keys: list[str]) -> dict[str, Ed25519PublicKey]:
    keys = [load_public_key(k) for k in hex_keys]
    return {key_id(k): k for k in keys}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis_core.audit")
    parser.add_argument("--domain", type=Domain, default=Domain.CORE)
    parser.add_argument("--secrets-root", type=Path, default=Path("/run/jarvis/secrets"))
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("checkpoint", "export", "verify"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--file", type=Path, default=None, help="a domain's JSONL chain")
        cmd.add_argument("--dsn", default=None, help="pg-audit DSN without the password")
        cmd.add_argument("--password-file", type=Path, default=None)
        if name == "export":
            cmd.add_argument("--out", type=Path, required=True)
        if name == "verify":
            cmd.add_argument("--public-key", action="append", default=[], required=True)
            cmd.add_argument("--bundles", type=Path, default=None, help="verify exported bundles")
    sub.add_parser("public-key")
    args = parser.parse_args(argv)

    provider = UnpackedSopsProvider(args.domain, root=args.secrets_root)
    if args.command == "public-key":
        key = load_private_key(provider.get(f"{args.domain.value}/audit_signing_key"))
        print(public_key_hex(key.public_key()))
        return 0

    if args.command == "verify" and args.bundles is not None:
        problems = verify_bundles(args.bundles, _keys(args.public_key))
        for problem in problems:
            print(problem, file=sys.stderr)
        print("bundles intact" if not problems else f"{len(problems)} problems")
        return 0 if not problems else 1

    entries, checkpoints, count, head = _open(args)
    if args.command == "checkpoint":
        key = load_private_key(provider.get(f"{args.domain.value}/audit_signing_key"))
        try:
            cp = next_checkpoint(count, head, checkpoints, key, datetime.now(UTC))
        except CheckpointError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        print(f"signed {cp.upto_seq} entries, head {cp.head_hash[:12]}, key {cp.key_id}")
        return 0
    if args.command == "export":
        manifest = export_bundle(entries, checkpoints.all(), args.out)
        if manifest is None:
            print("nothing new to export")
        else:
            print(f"exported entries {manifest.first_seq}-{manifest.last_seq}")
        return 0
    findings = verify_log(entries, checkpoints.all(), _keys(args.public_key))
    for finding in findings:
        print(f"{finding.kind} at {finding.index}: {finding.detail}", file=sys.stderr)
    print("log intact" if not findings else f"{len(findings)} problems")
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
