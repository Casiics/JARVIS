"""Smoke test of the supervisor-worker loop on the real local models.

    uv run python -m jarvis_core.agents smoke --ollama-url http://localhost:11434
    uv run python -m jarvis_core.agents smoke --runtime hermes   # needs `hermes` on PATH

Runs one small, public task (worker, Tier 0b, supervisor) in the content domain, local
models only and a cloud budget of zero, and prints the outcome as metadata. Nothing leaves
the machine; the audit chain is kept in memory and checked at the end.
"""

from __future__ import annotations

import argparse
import json
import secrets
import shlex
from datetime import timedelta
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from jarvis_core.agents.hermes import HermesRuntime
from jarvis_core.agents.runtime import AgentRuntime, GatewayRuntime
from jarvis_core.audit.chain import AuditWriter, InMemoryAuditStore, verify_chain
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.task_token import issue_token, verify_token
from jarvis_core.orchestrator.loop import (
    TOOL_SUPERVISOR,
    TOOL_WORKER,
    Acceptance,
    RuntimeSupervisor,
    RuntimeWorker,
    SupervisorWorkerLoop,
    WorkOrder,
)
from jarvis_core.orchestrator.state_machine import TaskMachine
from jarvis_core.router.budget import CircuitBreaker, InMemorySpendLedger
from jarvis_core.router.client import OllamaClient
from jarvis_core.router.gateway import InferenceGateway

TEXT = (
    "JARVIS is split into four domains: core, trading, content and private. Domains never "
    "talk to each other directly; typed events cross boundaries through core. Private data "
    "only ever reaches local models."
)


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=3, max_length=80)
    points: list[str] = Field(min_length=2, max_length=4)


def _mentions_private(summary: Summary) -> str | None:
    joined = " ".join(summary.points).lower()
    return (
        None if "private" in joined or "local" in joined else "mention how private data is handled"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis_core.agents")
    sub = parser.add_subparsers(dest="cmd", required=True)
    smoke = sub.add_parser("smoke", help="run one loop on the local models")
    smoke.add_argument("--ollama-url", default="http://localhost:11434")
    smoke.add_argument("--runtime", choices=("gateway", "hermes"), default="gateway")
    smoke.add_argument("--hermes-command", default="hermes", help="argv prefix, shell-quoted")
    args = parser.parse_args(argv)

    store = InMemoryAuditStore()
    writer = AuditWriter(store)
    gateway = InferenceGateway(
        local=OllamaClient(args.ollama_url),
        breaker=CircuitBreaker(InMemorySpendLedger()),
        audit=writer.write,
        actor="spiffe://jarvis.home/core/model-router",
    )
    runtime: AgentRuntime
    if args.runtime == "hermes":
        runtime = HermesRuntime(
            gateway, Domain.CONTENT, command=tuple(shlex.split(args.hermes_command))
        )
    else:
        runtime = GatewayRuntime(gateway, Domain.CONTENT, require_local=True)

    key = secrets.token_bytes(32)
    claims = verify_token(
        key,
        issue_token(
            key,
            task_id=uuid4(),
            domain=Domain.CONTENT,
            allowed_tools=frozenset({TOOL_WORKER, TOOL_SUPERVISOR}),
            budget_eur=0.0,
            ttl=timedelta(minutes=30),
        ),
    )
    loop = SupervisorWorkerLoop(
        TaskMachine(writer.write, "spiffe://jarvis.home/content/smoke"),
        telemetry=lambda e: print(f"  {e.node:<16} {e.state.value}", flush=True),
    )
    outcome = loop.run(
        WorkOrder(
            claims=claims,
            trace_id=uuid4().hex,
            instruction="Summarise the text in a short title and two to four bullet points.",
            label=DataClassification.PUBLIC,
            inputs=(("text", TEXT),),
        ),
        Acceptance(Summary, checks=(_mentions_private,)),
        RuntimeWorker(runtime),
        RuntimeSupervisor(runtime),
    )
    print(
        json.dumps(
            {
                "runtime": args.runtime,
                "state": outcome.state.value,
                "attempts": outcome.attempts,
                "tokens": outcome.tokens,
                "cost_eur": outcome.cost_eur,
                "error_class": outcome.error_class,
                "result": outcome.value.model_dump() if outcome.value else None,
                "audit_entries": len(store.entries),
                "audit_chain_intact": verify_chain(store.entries) is None,
            },
            indent=2,
        )
    )
    return 0 if outcome.value is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
