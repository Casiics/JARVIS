"""Run the private domain's assistant (inside the private-assistant container).

    docker compose -f docker/docker-compose.yml run --rm private-assistant daily
    docker compose -f docker/docker-compose.yml run --rm private-assistant \
        reply <mail_id> "Agree, Thursday works"

Commands: sync (calendar and new mail), triage (sort new mail), briefing (today's overview),
daily (all three), mail (list recent mail ids), reply (draft a reply; it waits for approval and
is never sent), drafts (list drafts). Everything runs on local models; nothing is sent.

Configuration: JARVIS_CALDAV_URL (calendar collection URL), JARVIS_IMAP_HOST, JARVIS_IMAP_PORT,
JARVIS_OWNER_ADDRESS, JARVIS_TZ, JARVIS_OLLAMA_URL. Credentials come from the private domain's
secrets: caldav_username, caldav_password, imap_username, imap_password.

Until the core task router runs as a service, this command issues its own short-lived task
token with an ephemeral key, as the reel CLI does, so every step is still scoped by a token.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from jarvis_core.audit.chain import AuditWriter
from jarvis_core.audit.file import FileAuditStore
from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.events import Taint
from jarvis_core.contracts.task_token import TaskTokenClaims, issue_token, verify_token
from jarvis_core.router.budget import CircuitBreaker, FileSpendLedger
from jarvis_core.router.client import OllamaClient
from jarvis_core.router.gateway import InferenceGateway
from jarvis_core.secrets.provider import SecretAccessError, UnpackedSopsProvider
from mod_private.assistant import PRIVATE_TOOLS, PrivateAssistant
from mod_private.caldav import CalDAVClient
from mod_private.imap import ImapMailbox
from mod_private.store import PrivateStore
from mod_private.supervisor import LocalSupervisor
from mod_private.workers import PrivateWorkers


def credential(provider: UnpackedSopsProvider, key: str) -> str:
    """Account credentials start empty after bootstrap; refuse them instead of logging in blank."""
    value = provider.get(f"private/{key}")
    if not value:
        raise SecretAccessError(f"secret 'private/{key}' is empty; set it with sops")
    return value


def build(args: argparse.Namespace) -> PrivateAssistant:
    tz = ZoneInfo(os.environ.get("JARVIS_TZ", "Europe/Berlin"))
    data: Path = args.data_dir
    store = PrivateStore(data)
    audit = AuditWriter(FileAuditStore(data / "audit.jsonl")).write
    gateway = InferenceGateway(
        local=OllamaClient(os.environ.get("JARVIS_OLLAMA_URL", "http://ollama:11434")),
        breaker=CircuitBreaker(FileSpendLedger(data / "spend.jsonl")),
        audit=audit,
        actor="spiffe://jarvis.home/private/model-router",
    )
    provider = UnpackedSopsProvider(Domain.PRIVATE, root=args.secrets_root)
    calendar = None
    if url := os.environ.get("JARVIS_CALDAV_URL"):
        calendar = CalDAVClient(
            url,
            credential(provider, "caldav_username"),
            credential(provider, "caldav_password"),
            default_tz=tz,
        )
    mailbox = None
    if host := os.environ.get("JARVIS_IMAP_HOST"):
        mailbox = ImapMailbox(
            host,
            credential(provider, "imap_username"),
            credential(provider, "imap_password"),
            port=int(os.environ.get("JARVIS_IMAP_PORT", "993")),
        )
    return PrivateAssistant(
        store=store,
        workers=PrivateWorkers(gateway),
        supervisor=LocalSupervisor(gateway),
        audit=audit,
        calendar=calendar,
        mailbox=mailbox,
        owner_address=os.environ.get("JARVIS_OWNER_ADDRESS", ""),
        tz=tz,
    )


def claims() -> TaskTokenClaims:
    key = secrets.token_bytes(32)
    token = issue_token(
        key,
        task_id=uuid4(),
        domain=Domain.PRIVATE,
        allowed_tools=PRIVATE_TOOLS,
        budget_eur=0.0,
        taint=Taint.EXTERNAL_CONTENT,
        ttl=timedelta(minutes=30),
    )
    return verify_token(key, token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mod_private")
    parser.add_argument("--data-dir", type=Path, default=Path("/var/lib/jarvis/private"))
    parser.add_argument("--secrets-root", type=Path, default=Path("/run/jarvis/secrets"))
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("sync", "triage", "daily", "mail", "drafts"):
        sub.add_parser(name)
    brief = sub.add_parser("briefing")
    brief.add_argument("--date", type=date.fromisoformat, default=None)
    reply = sub.add_parser("reply")
    reply.add_argument("mail_id")
    reply.add_argument("instruction")
    args = parser.parse_args(argv)

    try:
        assistant = build(args)
    except SecretAccessError as exc:
        print(f"missing secret: {exc}", file=sys.stderr)
        return 2
    scope = claims()
    today = datetime.now(assistant.tz).date()

    if args.command in ("sync", "daily"):
        report = assistant.sync(scope)
        print(f"synced {report.events} events, {report.new_mails} new mails")
    if args.command in ("triage", "daily"):
        print(f"sorted {len(assistant.triage_new(scope))} mails")
    if args.command in ("briefing", "daily"):
        day = getattr(args, "date", None) or today
        briefing = assistant.briefing(scope, day)
        if briefing is None:
            print("no briefing: the local workers did not produce one that passed review")
            return 1
        print(f"\n{day.isoformat()}: {briefing.headline}")
        for item in briefing.items:
            print(f"  - {item.text}")
    if args.command == "mail":
        for mail in sorted(assistant.store.mails.all(), key=lambda m: m.uid, reverse=True)[:30]:
            triage = assistant.store.triage.get(mail.mail_id)
            label = triage.category.value if triage else "unsorted"
            print(f"{mail.mail_id}  [{label}]  {mail.sender[:40]}  {mail.subject[:60]}")
    if args.command == "reply":
        proposal = assistant.propose_reply(scope, args.mail_id, args.instruction)
        if proposal is None:
            print("no draft: the local workers did not produce one that passed review")
            return 1
        print(f"To: {', '.join(proposal.draft.to)}\nSubject: {proposal.draft.subject}\n")
        print(proposal.draft.body)
        print(f"\ndraft {proposal.draft.draft_id}: {proposal.decision.outcome.value}")
    if args.command == "drafts":
        for draft in assistant.store.drafts.all():
            print(f"{draft.draft_id}  {draft.status.value}  {draft.subject[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
