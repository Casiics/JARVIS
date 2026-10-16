"""Propose a patch through the git gateway.

    uv run --extra gitgateway --extra db python -m jarvis_core.gitgateway \\
        --repo Casiics/JARVIS --patch change.diff --topic tighten-reel-gate \\
        --title "Tighten the reel gate" --task-id <uuid> --audit-dsn "$JARVIS_AUDIT_WRITER_DSN"

GitHub App credentials come from the core domain's unpacked secrets: github_app_id,
github_app_installation_id and github_app_private_key (PEM). Self-modification is always
audited, so a pg-audit writer DSN is required.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID, uuid4

from jarvis_core.audit.chain import AuditWriter
from jarvis_core.contracts.classification import Domain
from jarvis_core.gitgateway.gateway import GitGateway, Proposal, ProposalRejected
from jarvis_core.gitgateway.github import GitHubApp, GitHubPullRequests, Repository
from jarvis_core.secrets.provider import UnpackedSopsProvider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis_core.gitgateway")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--base", default="main")
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--body-file", type=Path, default=None)
    parser.add_argument("--task-id", type=UUID, required=True)
    parser.add_argument("--trace-id", default=None)
    parser.add_argument("--secrets-root", type=Path, default=Path("/run/jarvis/secrets"))
    parser.add_argument("--audit-dsn", required=True, help="pg-audit writer DSN")
    args = parser.parse_args(argv)

    owner, _, name = args.repo.partition("/")
    repository = Repository(owner, name)
    secrets = UnpackedSopsProvider(Domain.CORE, root=args.secrets_root)
    app = GitHubApp(
        app_id=secrets.get("core/github_app_id"),
        installation_id=secrets.get("core/github_app_installation_id"),
        private_key_pem=secrets.get("core/github_app_private_key").encode(),
        repository=repository,
    )

    import psycopg

    from jarvis_core.audit.postgres import PostgresAuditStore

    gateway = GitGateway(
        remote=repository.https_url,
        base=args.base,
        pulls=GitHubPullRequests(repository),
        audit=AuditWriter(PostgresAuditStore(psycopg.connect(args.audit_dsn))).write,
        token=app.installation_token,
    )
    proposal = Proposal(
        topic=args.topic,
        title=args.title,
        body=args.body_file.read_text() if args.body_file else "",
        patch=args.patch.read_text(),
        task_id=args.task_id,
        trace_id=args.trace_id or uuid4().hex,
    )
    try:
        result = gateway.propose(proposal)
    except ProposalRejected as exc:
        for violation in exc.violations:
            print(f"rejected: {violation.reason.value} {violation.path}", file=sys.stderr)
        return 2
    print(f"{result.branch} {result.commit} {result.pull_request}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
