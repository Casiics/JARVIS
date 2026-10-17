"""Run a research session with workers on the local models.

    uv run python -m trading_research --data-dir /var/lib/jarvis/trading \\
        run --bars spy-daily.csv qqq-daily.csv --ollama-url http://localhost:11434
    uv run python -m trading_research --data-dir /var/lib/jarvis/trading ledger

`run` uses local models only and a cloud budget of zero. What the workers propose is judged
by `mod_trading.lab`; candidates land in `<data-dir>/research/candidates/`, never on the
paper account.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from jarvis_core.agents.runtime import GatewayRuntime
from jarvis_core.audit.chain import AuditWriter
from jarvis_core.audit.file import FileAuditStore
from jarvis_core.contracts.classification import Domain
from jarvis_core.orchestrator.loop import RuntimeWorker, SupervisorWorkerLoop
from jarvis_core.orchestrator.state_machine import TaskMachine
from jarvis_core.router.budget import CircuitBreaker, InMemorySpendLedger
from jarvis_core.router.client import OllamaClient
from jarvis_core.router.gateway import InferenceGateway
from mod_trading.lab import (
    ACTOR,
    Ledger,
    ResearchLab,
    Workbench,
    load_series,
    research_claims,
)
from mod_trading.metrics import bars_per_year
from mod_trading.research import ResearchGate
from mod_trading.signals import describe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trading_research")
    parser.add_argument("--data-dir", type=Path, default=Path("/var/lib/jarvis/trading"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="one research session on local models")
    run.add_argument("--bars", type=Path, nargs="+", required=True)
    run.add_argument("--ollama-url", default="http://localhost:11434")
    sub.add_parser("ledger", help="what was tested, and how it did")
    args = parser.parse_args(argv)
    research = args.data_dir / "research"
    ledger = Ledger(research / "ledger.jsonl")
    if args.cmd == "ledger":
        for e in ledger.entries():
            rule = describe(e.spec) if e.spec else "-"
            print(
                f"{e.at:%Y-%m-%d} {e.instrument:6} {e.stage:17} research {e.research_sharpe} "
                f"vs {e.baseline_research_sharpe}, holdout {e.holdout_sharpe} vs "
                f"{e.baseline_holdout_sharpe} (+{e.holdout_margin})  {rule}"
            )
        return 0
    series = load_series(args.bars)
    if not series:
        print("no bars", file=sys.stderr)
        return 2
    per_year = bars_per_year(next(iter(series.values())))
    writer = AuditWriter(FileAuditStore(args.data_dir / "audit.jsonl"))
    gateway = InferenceGateway(
        local=OllamaClient(args.ollama_url),
        breaker=CircuitBreaker(InMemorySpendLedger()),
        audit=writer.write,
        actor="spiffe://jarvis.home/core/model-router",
    )
    worker = RuntimeWorker(GatewayRuntime(gateway, Domain.TRADING, require_local=True))
    lab = ResearchLab(
        bench=Workbench(series, per_year, ResearchGate(bars_per_year=per_year)),
        ledger=ledger,
        candidates=research / "candidates",
        loop=SupervisorWorkerLoop(TaskMachine(writer.write, ACTOR)),
        researcher=worker,
        builder=worker,
        claims=research_claims,
        audit=writer.write,
    )
    for o in lab.run():
        print(f"{o.instrument:6} {o.stage:17} {o.hypothesis[:40]:40} {o.detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
