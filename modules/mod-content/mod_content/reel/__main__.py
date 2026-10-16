"""Workstation entry point for the reel pipeline.

    uv run python -m mod_content.reel ingest https://www.instagram.com/reel/<code>/
    uv run python -m mod_content.reel ingest --from-file reels.txt
    uv run python -m mod_content.reel report
    uv run python -m mod_content.reel serve      # reels submitted in the deck

Until the Telegram trigger in n8n-content and the core task router run as services
(Phase 4), this command plays both roles locally: it issues a short-lived task token with an
ephemeral key that never leaves the process, so every step is still scoped by a token.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sys
import time
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from jarvis_core.audit.chain import AuditChainError, AuditWriter, verify_chain
from jarvis_core.boundary.mailbox import Mailbox
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import BoundaryEvent, ReelSubmitted, Taint, TaskMetadata
from jarvis_core.contracts.reel_url import InvalidReelUrl, canonical_reel_url
from jarvis_core.contracts.task_token import issue_token
from jarvis_core.orchestrator.state_machine import TaskState
from jarvis_core.router.budget import CircuitBreaker, InMemorySpendLedger
from jarvis_core.router.client import OllamaClient
from jarvis_core.router.gateway import InferenceGateway
from mod_content.reel.pipeline import (
    REEL_TOOLS,
    CountingSink,
    ReelPipeline,
    SandboxImages,
    WhisperConfig,
)
from mod_content.reel.sandbox import EngineConfig, SubprocessSandboxRunner
from mod_content.reel.store import FileAuditStore, JsonlReelStore, summarize

DEFAULT_DATA = Path(os.environ.get("JARVIS_CONTENT_DATA", "/var/lib/jarvis/content"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mod_content.reel")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="process one or more reel URLs")
    ingest.add_argument("urls", nargs="*")
    ingest.add_argument("--from-file", type=Path)
    _pipeline_options(ingest)

    serve = sub.add_parser("serve", help="process reels submitted in the deck")
    serve.add_argument(
        "--boundary-dir",
        type=Path,
        default=Path("/var/lib/jarvis/boundary"),
        help="mailboxes shared with core (same as the API gateway's --boundary-dir)",
    )
    serve.add_argument("--poll-seconds", type=float, default=5.0)
    serve.add_argument("--once", action="store_true", help="handle waiting reels, then exit")
    _pipeline_options(serve)

    sub.add_parser("report", help="cost, outcome and audit summary of all runs")

    args = parser.parse_args(argv)
    data: Path = args.data_dir
    if args.command == "report":
        return _report(data)
    if args.command == "serve":
        return _serve(args, data)
    return _ingest(args, data)


def _pipeline_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ollama-url", default=os.environ.get("JARVIS_OLLAMA_URL", "http://127.0.0.1:11434")
    )
    parser.add_argument("--engine", choices=("podman", "docker"), default="podman")
    parser.add_argument("--runtime", default=None, help="e.g. runsc for gVisor")
    parser.add_argument("--egress-network", default="jarvis-sandbox-egress")
    parser.add_argument(
        "--egress-proxy",
        default="http://egress-reel:3128",
        help="egress proxy enforcing policies/egress/reel.allow (compose service egress-reel)",
    )
    parser.add_argument(
        "--whisper-models", type=Path, default=Path("/var/lib/jarvis/models/whisper")
    )
    parser.add_argument("--whisper-model", default="large-v3")
    parser.add_argument("--media-image", default=SandboxImages.media)
    parser.add_argument("--whisper-image", default=SandboxImages.whisper)
    parser.add_argument("--keep-media", action="store_true")


def _ingest(args: argparse.Namespace, data: Path) -> int:
    urls = list(args.urls)
    if args.from_file:
        urls += [
            line.strip()
            for line in args.from_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    if not urls:
        print("no URLs given", file=sys.stderr)
        return 2

    failures = 0
    valid: list[str] = []
    for url in urls:
        try:
            valid.append(canonical_reel_url(url))
        except InvalidReelUrl as exc:
            print(f"skipped: {exc}", file=sys.stderr)
            failures += 1
    if not valid:
        return 1

    pipeline, key = _build(args, data)
    store = JsonlReelStore(data / "runs.jsonl")
    for url in valid:
        record = pipeline.run(url, _token(key, uuid4()))
        store.append(record)
        print(
            f"{record.reel_id}: {record.state.value}"
            f"{' (' + record.error_class + ')' if record.error_class else ''}"
            f", {record.duration_ms / 1000:.1f}s, {record.cost_eur:.4f} EUR"
            f", follow-ups: {', '.join(record.follow_ups) or 'none'}"
        )
        failures += record.error_class is not None
    return 1 if failures else 0


def _build(
    args: argparse.Namespace, data: Path, mailbox: Mailbox | None = None
) -> tuple[ReelPipeline, bytes]:
    """The pipeline and its ephemeral token key. With a mailbox, status goes back to core."""
    if shutil.which(args.engine) is None:
        raise SystemExit(
            f"{args.engine} not found: the reel worker runs its sandboxes with it "
            "(docs/runbooks/reel-pipeline.md)"
        )
    audit = CountingSink(AuditWriter(FileAuditStore(data / "audit.jsonl")).write)
    gateway = InferenceGateway(
        local=OllamaClient(args.ollama_url),
        breaker=CircuitBreaker(InMemorySpendLedger()),
        audit=audit,
        actor="spiffe://jarvis.home/core/model-router",
    )
    runner = SubprocessSandboxRunner(
        EngineConfig(
            engine=args.engine,
            runtime=args.runtime,
            egress_network=args.egress_network,
            egress_proxy=args.egress_proxy,
        )
    )
    key = secrets.token_bytes(32)
    outbox_file = data / "outbox.jsonl"

    def outbox(event: BoundaryEvent) -> None:
        if mailbox is not None and isinstance(event, TaskMetadata):
            mailbox.send(event)  # status for the deck; core learns nothing else
            return
        with outbox_file.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"status": "pending", "event": event.model_dump(mode="json")}) + "\n"
            )

    pipeline = ReelPipeline(
        runner=runner,
        gateway=gateway,
        audit=audit,
        token_key=key,
        work_root=data / "work",
        whisper=WhisperConfig(models_dir=args.whisper_models, model=args.whisper_model),
        images=SandboxImages(media=args.media_image, whisper=args.whisper_image),
        outbox=outbox,
        keep_media=args.keep_media,
    )
    return pipeline, key


def _token(key: bytes, task_id: UUID) -> str:
    return issue_token(
        key,
        task_id=task_id,
        domain=Domain.CONTENT,
        allowed_tools=REEL_TOOLS,
        budget_eur=0.0,
        taint=Taint.EXTERNAL_CONTENT,
        ttl=timedelta(minutes=30),
    )


def _serve(args: argparse.Namespace, data: Path) -> int:
    mailbox = Mailbox(args.boundary_dir, Domain.CONTENT)
    pipeline, key = _build(args, data, mailbox)
    store = JsonlReelStore(data / "runs.jsonl")
    print(f"waiting for reels in {args.boundary_dir}", flush=True)
    while True:
        for letter in mailbox.receive(Domain.CORE):
            event = letter.event
            if isinstance(event, ReelSubmitted):
                _handle(mailbox, pipeline, store, key, event)
            mailbox.ack(letter)
        if args.once:
            return 0
        time.sleep(args.poll_seconds)


def _handle(
    mailbox: Mailbox,
    pipeline: ReelPipeline,
    store: JsonlReelStore,
    key: bytes,
    event: ReelSubmitted,
) -> None:
    started = time.monotonic()

    def report(state: str, error_class: str | None = None) -> None:
        mailbox.send(
            TaskMetadata(
                origin_domain=Domain.CONTENT,
                target_domain=Domain.CORE,
                classification=DataClassification.INTERNAL,
                task_id=event.task_id,
                state=state,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_class=error_class,
            )
        )

    report(TaskState.RUNNING.value)
    try:
        record = pipeline.run(event.source_ref, _token(key, event.task_id))
    except Exception as exc:
        print(f"{event.task_id}: failed ({type(exc).__name__})", file=sys.stderr, flush=True)
        report(TaskState.ESCALATED.value, "internal_error")
        return
    store.append(record)  # the pipeline itself reported the final state
    print(f"{record.reel_id}: {record.state.value}", flush=True)


def _report(data: Path) -> int:
    records = JsonlReelStore(data / "runs.jsonl").load()
    audit_file = data / "audit.jsonl"
    intact: bool | None = None
    if audit_file.exists():
        try:
            intact = verify_chain(FileAuditStore(audit_file).entries()) is None
        except AuditChainError:
            intact = False
    print(summarize(records, intact).model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
