"""Read-only view of the orchestrator's tasks for the terminal, and the task router's key.

    uv run python -m jarvis_core.orchestrator --data-dir /var/lib/jarvis/core/deck status
    uv run python -m jarvis_core.orchestrator --data-dir … status --json
    uv run python -m jarvis_core.orchestrator public-key [--secrets-root /run/jarvis/secrets]

Reads `tasks.json` as the API gateway writes it; it never sends or acknowledges events, so it
is safe to run next to the running gateway.

`public-key` prints the public half of core's `task_token_signing_key` (32-byte Ed25519 seed,
hex), which never leaves core. Workers get only this public key (`--task-public-key` /
JARVIS_TASK_PUBLIC_KEY).
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter

from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.task_token import load_signing_key, public_key_hex
from jarvis_core.orchestrator.service import TaskRecord
from jarvis_core.secrets.provider import UnpackedSopsProvider

_RECORDS = TypeAdapter(list[TaskRecord])


def _age(ts: datetime, now: datetime) -> str:
    seconds = int((now - ts).total_seconds())
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis_core.orchestrator")
    parser.add_argument("--data-dir", type=Path, default=Path("/var/lib/jarvis/core/deck"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    status = sub.add_parser("status", help="list tasks and counts per state")
    status.add_argument("--json", action="store_true")
    status.add_argument("--limit", type=int, default=20)
    show = sub.add_parser("public-key", help="print the task-token public key (hex)")
    show.add_argument("--secrets-root", type=Path, default=Path("/run/jarvis/secrets"))
    args = parser.parse_args(argv)

    if args.cmd == "public-key":
        provider = UnpackedSopsProvider(Domain.CORE, root=args.secrets_root)
        print(public_key_hex(load_signing_key(provider.get("core/task_token_signing_key"))))
        return 0

    path = args.data_dir / "tasks.json"
    tasks = _RECORDS.validate_json(path.read_bytes()) if path.exists() else []
    tasks.sort(key=lambda t: t.updated_at, reverse=True)
    counts = Counter(t.state.value for t in tasks)

    if args.json:
        print(
            json.dumps(
                {
                    "tasks": len(tasks),
                    "by_state": dict(sorted(counts.items())),
                    "recent": [t.model_dump(mode="json") for t in tasks[: args.limit]],
                },
                indent=2,
            )
        )
        return 0

    if not tasks:
        print(f"no tasks in {path}")
        return 0
    now = datetime.now(UTC)
    print("  ".join(f"{state}: {n}" for state, n in sorted(counts.items())))
    for t in tasks[: args.limit]:
        error = f"  {t.error_class}" if t.error_class else ""
        print(
            f"{str(t.task_id)[:8]}  {t.domain.value:<8} {t.kind:<6} {t.state.value:<15} "
            f"{_age(t.updated_at, now):>4} ago{error}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
