"""Run the API gateway.

    uv run --extra gateway python -m jarvis_core.api --rp-id jarvis.tailnet.ts.net \\
        --origin https://jarvis.tailnet.ts.net --static apps/deck/dist

Keys come from the core domain's unpacked secrets (approval_grant_key, deck_bootstrap_code).
The first passkey is registered with the bootstrap code; after that, new devices can only be
added from a signed-in session. TLS terminates at Caddy on the tailnet; bind to loopback or
net-gateway only.
"""

from __future__ import annotations

import argparse
import threading
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from jarvis_core.api.app import GatewayDeps, GatewaySettings, TelemetryHub, create_app
from jarvis_core.api.auth import sha256_hex
from jarvis_core.api.context_reviews import ContextReviewDesk
from jarvis_core.api.filestore import FileApprovalStore, FileCredentialStore
from jarvis_core.api.reels import ReelDesk
from jarvis_core.audit.chain import AuditStore, AuditWriter, InMemoryAuditStore
from jarvis_core.boundary.mailbox import Mailbox
from jarvis_core.contracts.audit import AuditLogEntry
from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.task_token import load_signing_key, public_key_hex
from jarvis_core.orchestrator.service import Orchestrator, TaskTokenIssuer, run_forever
from jarvis_core.secrets.provider import SecretAccessError, UnpackedSopsProvider


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m jarvis_core.api")
    parser.add_argument("--rp-id", required=True, help="tailnet host name of the deck")
    parser.add_argument("--origin", required=True, help="https origin the browser sees")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--static", type=Path, default=None, help="built deck (apps/deck/dist)")
    parser.add_argument("--secrets-root", type=Path, default=Path("/run/jarvis/secrets"))
    parser.add_argument("--data-dir", type=Path, default=Path("/var/lib/jarvis/core/deck"))
    parser.add_argument("--audit-dsn", default=None, help="pg-audit writer DSN")
    parser.add_argument(
        "--boundary-dir",
        type=Path,
        default=Path("/var/lib/jarvis/boundary"),
        help="mailboxes shared with the domain processes (reel intake)",
    )
    parser.add_argument("--tick-seconds", type=float, default=2.0, help="orchestrator poll")
    args = parser.parse_args(argv)

    secrets = UnpackedSopsProvider(Domain.CORE, root=args.secrets_root)
    settings = GatewaySettings(
        grant_key=secrets.get("core/approval_grant_key").encode(),
        rp_id=args.rp_id,
        origin=args.origin,
        bootstrap_code_hash=sha256_hex(secrets.get("core/deck_bootstrap_code")),
        static_dir=args.static,
        secure_cookies=args.origin.startswith("https://"),
    )
    # Credentials, approvals and tasks move to pg-core behind the same interfaces.
    writer = AuditWriter(_audit_store(args.audit_dsn))
    audit_lock = threading.Lock()

    # Request threads and the orchestrator loop both audit; head() and append() must not
    # interleave, or two entries would chain onto the same predecessor.
    def audit(entry: AuditLogEntry) -> None:
        with audit_lock:
            writer.write(entry)

    clock = lambda: datetime.now(UTC)  # noqa: E731
    telemetry = TelemetryHub()
    orchestrator = Orchestrator(
        Mailbox(args.boundary_dir, Domain.CORE),
        args.data_dir / "tasks.json",
        clock=clock,
        audit=audit,
        telemetry=telemetry.publish,
        tokens=_task_tokens(secrets),
    )
    deps = GatewayDeps(
        credentials=FileCredentialStore(args.data_dir / "credentials.json"),
        approvals=FileApprovalStore(args.data_dir / "approvals.json"),
        audit=audit,
        telemetry=telemetry,
        reels=ReelDesk(orchestrator, args.data_dir / "reels.json", clock=clock),
        orchestrator=orchestrator,
    )
    # Domain reports reach the deck's live view without anyone polling the API.
    threading.Thread(
        target=run_forever, args=(orchestrator, args.tick_seconds), daemon=True
    ).start()

    # Own mailbox root: the reel desk consumes every letter in the main content-to-core box.
    deps.context_reviews = ContextReviewDesk(
        Mailbox(args.boundary_dir / "context-review", Domain.CORE),
        args.data_dir / "context-reviews.json",
        clock=lambda: datetime.now(UTC),
    )
    uvicorn.run(create_app(settings, deps), host=args.host, port=args.port, proxy_headers=True)


def _task_tokens(secrets: UnpackedSopsProvider) -> TaskTokenIssuer | None:
    """The task router's signing key. Without it, workers refuse every dispatched task."""
    try:
        key = load_signing_key(secrets.get("core/task_token_signing_key"))
    except SecretAccessError:
        print("warning: no core/task_token_signing_key, workers will refuse dispatched tasks")
        return None
    print(f"task-token public key: {public_key_hex(key)}")
    return TaskTokenIssuer(key)


def _audit_store(dsn: str | None) -> AuditStore:
    if dsn is None:
        print("warning: no --audit-dsn, audit entries are kept in memory only")
        return InMemoryAuditStore()
    import psycopg

    from jarvis_core.audit.postgres import PostgresAuditStore

    return PostgresAuditStore(psycopg.connect(dsn))


if __name__ == "__main__":
    main()
