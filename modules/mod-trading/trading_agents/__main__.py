"""Trading's agents, from the command line.

    uv run python -m trading_agents review-ideas     # assess open inbox ideas
    uv run python -m trading_agents ideas            # open ideas with their latest verdict

Assessments use local models only. Hermes Agent is the runtime when it is installed
(`--runtime auto`); otherwise the inference gateway answers directly. The review token is
issued here with an ephemeral key, as the other domain CLIs do, and allows the loop's two
steps only: a review never places an order or dismisses an idea.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from jarvis_core.agents.select import RuntimeUnavailable, local_runtime, runtime_name
from jarvis_core.audit.chain import AuditWriter
from jarvis_core.audit.file import FileAuditStore
from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.events import Taint
from jarvis_core.contracts.task_token import TaskTokenClaims, issue_token, verify_token
from jarvis_core.orchestrator.loop import (
    TOOL_SUPERVISOR,
    TOOL_WORKER,
    RuntimeSupervisor,
    RuntimeWorker,
    SupervisorWorkerLoop,
)
from jarvis_core.orchestrator.state_machine import TaskMachine
from jarvis_core.router.budget import CircuitBreaker, FileSpendLedger
from jarvis_core.router.client import OllamaClient
from jarvis_core.router.gateway import InferenceGateway
from mod_trading.inbox import TradingInbox
from trading_agents.review import AssessmentStore, IdeaReviewer, review_open_ideas


def review_claims() -> TaskTokenClaims:
    """Ideas are external content: the token is tainted and allows the loop's steps only."""
    key = secrets.token_bytes(32)
    token = issue_token(
        key,
        task_id=uuid4(),
        domain=Domain.TRADING,
        allowed_tools=frozenset({TOOL_WORKER, TOOL_SUPERVISOR}),
        budget_eur=0.0,
        taint=Taint.EXTERNAL_CONTENT,
        ttl=timedelta(minutes=30),
    )
    return verify_token(key, token)


def review_ideas(args: argparse.Namespace) -> int:
    data: Path = args.data_dir
    audit = AuditWriter(FileAuditStore(data / "agents-audit.jsonl")).write
    gateway = InferenceGateway(
        local=OllamaClient(args.ollama_url),
        breaker=CircuitBreaker(FileSpendLedger(data / "agents-spend.jsonl")),
        audit=audit,
        actor="spiffe://jarvis.home/trading/model-router",
    )
    try:
        runtime = local_runtime(gateway, Domain.TRADING, choice=args.runtime)
    except RuntimeUnavailable as exc:
        print(f"runtime unavailable: {exc}", file=sys.stderr)
        return 2
    reviewer = IdeaReviewer(
        loop=SupervisorWorkerLoop(
            TaskMachine(audit, actor="spiffe://jarvis.home/trading/idea-review")
        ),
        worker=RuntimeWorker(runtime),
        supervisor=RuntimeSupervisor(runtime),
        claims=review_claims,
        runtime=runtime_name(runtime),
    )
    results = review_open_ideas(
        TradingInbox(data / "ideas.json"),
        AssessmentStore(data / "assessments.json"),
        reviewer,
        limit=args.limit,
        again=args.again,
    )
    for r in results:
        verdict = r.assessment.verdict.value if r.assessment else (r.error_class or "-")
        print(f"{r.idea_id}  {r.state.value:<10} {verdict:<10} tries {r.attempts} ({r.runtime})")
    print(f"reviewed {len(results)} ideas")
    return 0


def list_ideas(args: argparse.Namespace) -> int:
    assessed = AssessmentStore(args.data_dir / "assessments.json").latest()
    for idea in TradingInbox(args.data_dir / "ideas.json").all():
        if idea.dismissed:
            continue
        seen = assessed.get(idea.idea_id)
        mark = seen.assessment.verdict.value if seen and seen.assessment else "open"
        print(f"{idea.idea_id}  [{mark}]  {idea.summary[:80]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trading_agents")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("JARVIS_TRADING_DATA", "/var/lib/jarvis/trading")),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    review = sub.add_parser("review-ideas", help="assess open ideas with local agents")
    review.add_argument("--runtime", choices=["auto", "hermes", "gateway"], default="auto")
    review.add_argument(
        "--ollama-url", default=os.environ.get("JARVIS_OLLAMA_URL", "http://ollama:11434")
    )
    review.add_argument("--limit", type=int, default=10)
    review.add_argument("--again", action="store_true", help="also re-assess reviewed ideas")
    sub.add_parser("ideas", help="open ideas with their latest verdict")
    args = parser.parse_args(argv)
    if args.command == "review-ideas":
        return review_ideas(args)
    return list_ideas(args)


if __name__ == "__main__":
    sys.exit(main())
