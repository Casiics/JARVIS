"""Trading domain command line (paper only).

    python -m mod_trading backtest --bars bars.csv --instrument SPY
    python -m mod_trading paper --bars today.csv --instrument SPY     # persistent paper account
    python -m mod_trading paper-live --instrument SPY                 # live bars through n8n
    python -m mod_trading history --instrument SPY --out spy-daily.csv  # daily bars, backtests
    python -m mod_trading status
    python -m mod_trading kill-switch status|engage REASON|release
    python -m mod_trading ideas

`backtest` replays bars against a fresh paper account with a released kill switch in memory.
`paper-live` polls 1-minute bars from the n8n-trading MCP tool (mod_trading.marketdata) and
trades them on the same paper account, against the wall clock, so stale data fails the risk
engine's quote-age check. `paper` and `paper-live` keep account, equity tracker, kill switch
and audit chain in --data-dir; the kill switch starts engaged (fail closed) until a human
runs `kill-switch release`.

Until the core task router runs as a service, each order is scoped by a short-lived task token
issued here with an ephemeral key, as the other domain CLIs do.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from jarvis_core.audit.chain import AuditWriter, InMemoryAuditStore
from jarvis_core.audit.file import FileAuditStore
from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.task_token import TaskTokenClaims, issue_token, verify_token
from mod_trading.account import EquityTracker
from mod_trading.broker import PaperBroker
from mod_trading.executor import TRADING_TOOLS, OrderExecutor
from mod_trading.feed import read_bars, replay
from mod_trading.inbox import TradingInbox
from mod_trading.marketdata import BarRecorder, LiveFeed, MarketDataError, McpBarSource
from mod_trading.session import SessionReport, run_session
from mod_trading.strategies import SmaCrossover
from trading_risk import KillSwitch, RiskEngine, RiskLimits

DEFAULT_LIMITS = Path(__file__).resolve().parents[1] / "risk" / "limits.json"


class SimClock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def set(self, moment: datetime) -> None:
        self.now = moment


def claims() -> TaskTokenClaims:
    key = secrets.token_bytes(32)
    token = issue_token(
        key,
        task_id=uuid4(),
        domain=Domain.TRADING,
        allowed_tools=TRADING_TOOLS,
        budget_eur=0.0,
        ttl=timedelta(minutes=5),
    )
    return verify_token(key, token)


def load_limits(path: Path) -> RiskLimits:
    return RiskLimits.model_validate_json(path.read_text("utf-8"))


def print_report(report: SessionReport) -> None:
    print(f"bars {report.bars}, intents {report.intents}")
    for outcome, count in sorted(report.outcomes.items()):
        print(f"  {outcome}: {count}")
    for code, count in sorted(report.violations.items()):
        print(f"  risk {code}: {count}")
    print(f"kill switch trips {report.kill_switch_trips}")
    print(
        f"equity {report.start_equity} -> {report.end_equity}, max drawdown "
        f"{report.max_drawdown:.2%}"
    )


def session(args: argparse.Namespace, *, persistent: bool) -> int:
    clock = SimClock()
    built = _executor(args, clock, persistent=persistent)
    if built is None:
        return 3
    executor, strategy = built
    bars = read_bars(args.bars)
    report = run_session(replay(bars, args.spread_bps), strategy, executor, claims, clock.set)
    print_report(report)
    return 0


def live(args: argparse.Namespace) -> int:
    def wall() -> datetime:
        return datetime.now(UTC)

    built = _executor(args, wall, persistent=True)
    if built is None:
        return 3
    executor, strategy = built
    feed = LiveFeed(
        source=McpBarSource(args.mcp_url),
        instrument=args.instrument,
        spread_bps=args.spread_bps,
        poll_s=args.poll_s,
        clock=wall,
        on_error=lambda msg: print(f"market data: {msg}", file=sys.stderr, flush=True),
        record=BarRecorder(args.data_dir / "bars"),
        max_polls=1 if args.once else None,
    )
    history = feed.warmup()
    if history:
        # Prime the strategy's averages; orders on these old bars would only fail the
        # quote-age check, so their intents are dropped.
        account = executor.account_view(wall())
        for bar in history:
            strategy.on_bar(bar, account)
    print(f"{args.instrument}: {len(history)} bars of history, polling every {args.poll_s:g} s")
    report = run_session(feed.stream(), strategy, executor, claims, lambda _ts: None)
    print_report(report)
    return 0


def history_csv(args: argparse.Namespace) -> int:
    try:
        bars = McpBarSource(args.mcp_url).daily(args.instrument, datetime.now(UTC))
    except MarketDataError as exc:
        print(f"market data: {exc}", file=sys.stderr)
        return 1
    args.out.unlink(missing_ok=True)
    record = BarRecorder(args.out.parent, name=args.out.name)
    for bar in bars:
        record(bar)
    print(f"{len(bars)} daily bars of {args.instrument} written to {args.out}")
    return 0


def _executor(
    args: argparse.Namespace, clock: Callable[[], datetime], *, persistent: bool
) -> tuple[OrderExecutor, SmaCrossover] | None:
    tz = ZoneInfo(args.tz)
    limits = load_limits(args.limits)
    if persistent:
        data: Path = args.data_dir
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
        broker = PaperBroker(state_path=data / "paper.json", clock=clock, starting_cash=args.cash)
        tracker = EquityTracker(tz, data / "tracker.json")
        kill_switch = KillSwitch(data / "killswitch")
        audit = AuditWriter(FileAuditStore(data / "audit.jsonl")).write
    else:
        scratch = Path(tempfile.mkdtemp(prefix="jarvis-backtest-"))
        broker = PaperBroker(clock=clock, starting_cash=args.cash)
        tracker = EquityTracker(tz)
        kill_switch = KillSwitch(scratch / "killswitch")
        kill_switch.release()
        audit = AuditWriter(InMemoryAuditStore()).write
    if persistent and kill_switch.engaged():
        print(f"kill switch engaged: {kill_switch.state().reason}", file=sys.stderr)
        return None
    executor = OrderExecutor(
        broker=broker,
        engine=RiskEngine(limits),
        kill_switch=kill_switch,
        tracker=tracker,
        audit=audit,
        clock=clock,
    )
    strategy = SmaCrossover(
        args.instrument,
        fast=args.fast,
        slow=args.slow,
        notional=args.notional,
        quantity_step=args.step,
    )
    return executor, strategy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mod_trading")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("JARVIS_TRADING_DATA", "/var/lib/jarvis/trading")),
    )
    parser.add_argument(
        "--limits",
        type=Path,
        default=Path(os.environ.get("JARVIS_RISK_LIMITS", str(DEFAULT_LIMITS))),
    )
    parser.add_argument("--tz", default=os.environ.get("JARVIS_TZ", "Europe/Berlin"))
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("backtest", "paper", "paper-live"):
        run = sub.add_parser(name)
        if name == "paper-live":
            run.add_argument(
                "--mcp-url",
                default=os.environ.get(
                    "JARVIS_MARKETDATA_MCP", "http://mcp-gateway-trading:8080/mcp/marketdata"
                ),
            )
            run.add_argument("--poll-s", type=float, default=60.0)
            run.add_argument("--once", action="store_true", help="poll once, then stop")
        else:
            run.add_argument("--bars", type=Path, required=True)
        run.add_argument("--instrument", required=True)
        run.add_argument("--fast", type=int, default=10)
        run.add_argument("--slow", type=int, default=30)
        run.add_argument("--notional", type=Decimal, default=Decimal(1000))
        run.add_argument("--step", type=Decimal, default=Decimal(1))
        run.add_argument("--cash", type=Decimal, default=Decimal(10_000))
        run.add_argument("--spread-bps", type=Decimal, default=Decimal(4))
    history = sub.add_parser("history")
    history.add_argument("--instrument", required=True)
    history.add_argument("--out", type=Path, required=True)
    history.add_argument(
        "--mcp-url",
        default=os.environ.get(
            "JARVIS_MARKETDATA_MCP", "http://mcp-gateway-trading:8080/mcp/marketdata"
        ),
    )
    sub.add_parser("status")
    sub.add_parser("ideas")
    ks = sub.add_parser("kill-switch")
    ks.add_argument("action", choices=["status", "engage", "release"])
    ks.add_argument("reason", nargs="?", default="manual")
    args = parser.parse_args(argv)

    if args.command == "backtest":
        return session(args, persistent=False)
    if args.command == "paper":
        return session(args, persistent=True)
    if args.command == "paper-live":
        return live(args)
    if args.command == "history":
        return history_csv(args)
    data = args.data_dir
    if args.command == "kill-switch":
        switch = KillSwitch(data / "killswitch")
        if args.action == "engage":
            switch.engage(args.reason)
        elif args.action == "release":
            switch.release()
        state = switch.state()
        print("engaged" if state.engaged else "released", state.reason)
        return 0
    if args.command == "status":
        broker = PaperBroker(state_path=data / "paper.json")
        print(f"cash {broker.state.cash}, fees {broker.state.fees_paid}")
        for instrument, quantity in sorted(broker.get_positions().items()):
            print(f"  {instrument}: {quantity}")
        print(f"orders {len(broker.state.orders)}, fills {len(broker.state.fills)}")
        state = KillSwitch(data / "killswitch").state()
        print(f"kill switch {'engaged' if state.engaged else 'released'} {state.reason}")
        return 0
    if args.command == "ideas":
        for idea in TradingInbox(data / "ideas.json").all():
            if not idea.dismissed:
                print(f"{idea.idea_id}  [{idea.origin}, {idea.taint}]  {idea.summary[:80]}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
