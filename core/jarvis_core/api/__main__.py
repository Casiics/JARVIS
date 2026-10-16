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
from pathlib import Path

import uvicorn

from jarvis_core.api.app import GatewayDeps, GatewaySettings, create_app
from jarvis_core.api.auth import sha256_hex
from jarvis_core.api.filestore import FileApprovalStore, FileCredentialStore
from jarvis_core.audit.chain import AuditStore, AuditWriter, InMemoryAuditStore
from jarvis_core.contracts.classification import Domain
from jarvis_core.secrets.provider import UnpackedSopsProvider


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
    # Credentials and approvals move to pg-core with the Phase 4 services.
    deps = GatewayDeps(
        credentials=FileCredentialStore(args.data_dir / "credentials.json"),
        approvals=FileApprovalStore(args.data_dir / "approvals.json"),
        audit=AuditWriter(_audit_store(args.audit_dsn)).write,
    )
    uvicorn.run(create_app(settings, deps), host=args.host, port=args.port, proxy_headers=True)


def _audit_store(dsn: str | None) -> AuditStore:
    if dsn is None:
        print("warning: no --audit-dsn, audit entries are kept in memory only")
        return InMemoryAuditStore()
    import psycopg

    from jarvis_core.audit.postgres import PostgresAuditStore

    return PostgresAuditStore(psycopg.connect(dsn))


if __name__ == "__main__":
    main()
