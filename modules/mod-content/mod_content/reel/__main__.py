"""Workstation entry point for the reel pipeline.

    uv run python -m mod_content.reel ingest https://www.instagram.com/reel/<code>/
    uv run python -m mod_content.reel ingest --from-file reels.txt
    uv run python -m mod_content.reel report

Until the Telegram trigger in n8n-content and the core task router run as services
(Phase 4), this command plays both roles locally: it issues a short-lived task token with an
ephemeral key that never leaves the process, so every step is still scoped by a token.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from jarvis_core.audit.chain import AuditChainError, AuditWriter, verify_chain
from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.events import BoundaryEvent, Taint
from jarvis_core.contracts.task_token import issue_token
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
from mod_content.reel.schemas import InvalidReelUrl
from mod_content.reel.store import FileAuditStore, JsonlReelStore, summarize

DEFAULT_DATA = Path(os.environ.get("JARVIS_CONTENT_DATA", "/var/lib/jarvis/content"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mod_content.reel")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="process one or more reel URLs")
    ingest.add_argument("urls", nargs="*")
    ingest.add_argument("--from-file", type=Path)
    ingest.add_argument(
        "--ollama-url", default=os.environ.get("JARVIS_OLLAMA_URL", "http://127.0.0.1:11434")
    )
    ingest.add_argument("--engine", choices=("podman", "docker"), default="podman")
    ingest.add_argument("--runtime", default=None, help="e.g. runsc for gVisor")
    ingest.add_argument("--egress-network", default="jarvis-sandbox-egress")
    ingest.add_argument(
        "--egress-proxy",
        default="http://egress-reel:3128",
        help="egress proxy enforcing policies/egress/reel.allow (compose service egress-reel)",
    )
    ingest.add_argument(
        "--whisper-models", type=Path, default=Path("/var/lib/jarvis/models/whisper")
    )
    ingest.add_argument("--whisper-model", default="large-v3")
    ingest.add_argument("--media-image", default=SandboxImages.media)
    ingest.add_argument("--whisper-image", default=SandboxImages.whisper)
    ingest.add_argument("--keep-media", action="store_true")

    sub.add_parser("report", help="cost, outcome and audit summary of all runs")

    args = parser.parse_args(argv)
    data: Path = args.data_dir
    if args.command == "report":
        return _report(data)
    return _ingest(args, data)


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
    store = JsonlReelStore(data / "runs.jsonl")
    failures = 0
    for url in urls:
        token = issue_token(
            key,
            task_id=uuid4(),
            domain=Domain.CONTENT,
            allowed_tools=REEL_TOOLS,
            budget_eur=0.0,
            taint=Taint.EXTERNAL_CONTENT,
            ttl=timedelta(minutes=30),
        )
        try:
            record = pipeline.run(url, token)
        except InvalidReelUrl as exc:
            print(f"skipped: {exc}", file=sys.stderr)
            failures += 1
            continue
        store.append(record)
        print(
            f"{record.reel_id}: {record.state.value}"
            f"{' (' + record.error_class + ')' if record.error_class else ''}"
            f", {record.duration_ms / 1000:.1f}s, {record.cost_eur:.4f} EUR"
            f", follow-ups: {', '.join(record.follow_ups) or 'none'}"
        )
        failures += record.error_class is not None
    return 1 if failures else 0


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
