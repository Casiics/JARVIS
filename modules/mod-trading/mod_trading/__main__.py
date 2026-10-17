"""Trading domain command line (paper only).

    python -m mod_trading backtest --bars bars.csv --instrument SPY
    python -m mod_trading backtest --bars spy.csv qqq.csv --instrument SPY,QQQ --compare
    python -m mod_trading paper --bars today.csv --instrument SPY     # persistent paper account
    python -m mod_trading paper-live --instrument SPY                 # live bars through n8n
    python -m mod_trading history --instrument SPY --out spy-daily.csv  # daily bars, backtests
    python -m mod_trading research-gate --bars spy-daily.csv --signal idea.csv --instrument SPY
    python -m mod_trading status
    python -m mod_trading kill-switch status|engage REASON|release
    python -m mod_trading ideas

`backtest` replays bars against a fresh paper account with a released kill switch in memory.
A comma-separated `--instrument` list trades a basket: one strategy per instrument, each with
its own `--capital` sleeve, all on the same account and under the same risk limits.
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
import time
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
from mod_trading.marketdata import (
    BarRecorder,
    BasketFeed,
    LiveFeed,
    MarketDataError,
    McpBarSource,
)
from mod_trading.metrics import Performance, bars_per_year, buy_and_hold, performance
from mod_trading.portfolio import (
    PortfolioError,
    aligned,
    basket_of_weights,
    correlations,
    split_instruments,
)
from mod_trading.research import (
    ResearchGate,
    SignalError,
    baseline_weights,
    read_signal,
    weight_curve,
)
from mod_trading.schemas import Bar
from mod_trading.session import SessionReport, run_session
from mod_trading.strategies import Basket, SmaCrossover, Strategy, TrendVolTarget
from trading_risk import KillSwitch, RiskEngine, RiskLimits

# The live feed delivers 1-minute bars of the regular session: 390 a day, 252 days a year.
LIVE_BARS_PER_YEAR = 252 * 390
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


SWEEP_TRENDS = (100, 150, 200, 250)
SWEEP_TARGET_VOLS = (0.08, 0.10, 0.15)


def sweep(args: argparse.Namespace) -> int:
    """The trend-vol baseline over a small parameter grid, next to buy-and-hold.

    A robustness check, not an optimiser: a setting is only credible when its neighbours do
    about as well. Every run is a fresh in-memory backtest through the same risk limits.
    """
    if len(args.instruments) != 1:
        print("--sweep takes one instrument", file=sys.stderr)
        return 2
    bars = _read_all(args.bars)
    per_year = bars_per_year(bars)
    rows: dict[str, Performance] = {}
    for trend in SWEEP_TRENDS:
        for target_vol in SWEEP_TARGET_VOLS:
            if trend <= args.vol_window:
                continue
            run = argparse.Namespace(**{**vars(args), "trend": trend, "target_vol": target_vol})
            clock = SimClock()
            built = _executor(run, clock, persistent=False, bar_frequency=per_year)
            if built is None:  # in-memory runs start released; only the setup can refuse
                return 2
            executor, strategy = built
            report = run_session(
                replay(bars, args.spread_bps), strategy, executor, claims, clock.set
            )
            if len(report.equity) < 2:
                continue
            refused = sum(report.violations.values())
            mark = "*" if (trend, target_vol) == (args.trend, args.target_vol) else " "
            name = f"{mark}{trend}/{target_vol:.2f}{'!' if refused else ''}"
            rows[name] = performance(report.equity, per_year)
    mine = [b for b in bars if b.instrument == args.instruments[0]]
    cost = args.spread_bps / 2 + Decimal(15)
    held = buy_and_hold(mine, cash=args.cash, capital=args.capital, cost_bps=cost)
    if len(held) >= 2:
        rows["buy_and_hold"] = performance(held, per_year)
    print("trend/target_vol; * = current settings, ! = some orders refused by the risk limits")
    print_comparison(rows)
    return 0


def session(args: argparse.Namespace, *, persistent: bool) -> int:
    if getattr(args, "sweep", False):
        return sweep(args)
    clock = SimClock()
    bars = _read_all(args.bars)
    built = _executor(args, clock, persistent=persistent, bar_frequency=bars_per_year(bars))
    if built is None:
        return 3
    executor, strategy = built
    report = run_session(replay(bars, args.spread_bps), strategy, executor, claims, clock.set)
    print_report(report)
    if getattr(args, "compare", False) and len(report.equity) >= 2:
        return compare(args, bars, report.equity, strategy.name)
    return 0


def compare(
    args: argparse.Namespace,
    bars: list[Bar],
    equity: list[tuple[datetime, Decimal]],
    name: str,
) -> int:
    """The account against buy-and-hold, then the signal itself per instrument and as a basket."""
    # Spread half plus the paper broker's slippage (5 bps) and fee (10 bps).
    cost = args.spread_bps / 2 + Decimal(15)
    try:
        series = aligned(bars, args.instruments)
    except PortfolioError as exc:
        print(f"compare: {exc}", file=sys.stderr)
        return 2
    per_year = bars_per_year(next(iter(series.values())))
    if len(args.instruments) == 1:
        mine = series[args.instruments[0]]
        held = buy_and_hold(mine, cash=args.cash, capital=args.capital, cost_bps=cost)
        print_comparison(
            {name: performance(equity, per_year), "buy_and_hold": performance(held, per_year)}
        )
    else:
        print_comparison({name: performance(equity, per_year)})
    if args.strategy != "trend-vol":
        return 0
    # The same decisions as compounding weights on each instrument, without the account's
    # absolute limits: what the signal itself is worth.
    print("signal per instrument (compounding, no account limits):")
    rows: dict[str, Performance] = {}
    signal: dict[str, tuple[list[Bar], list[float]]] = {}
    held_only: dict[str, tuple[list[Bar], list[float]]] = {}
    for instrument, mine in series.items():
        weights = baseline_weights(mine, **_baseline_params(args, per_year))
        signal[instrument] = (mine, weights)
        held_only[instrument] = (mine, [1.0] * len(mine))
        rows[f"{instrument}/trend-vol"] = performance(
            weight_curve(mine, weights, float(cost)), per_year
        )
        rows[f"{instrument}/hold"] = performance(
            weight_curve(mine, [1.0] * len(mine), float(cost)), per_year
        )
    if len(series) > 1:
        rows["basket/trend-vol"] = performance(basket_of_weights(signal, float(cost)), per_year)
        rows["basket/hold"] = performance(basket_of_weights(held_only, float(cost)), per_year)
    print_comparison(rows)
    if len(series) > 1:
        print("correlation of returns:")
        for (left, right), value in correlations(series).items():
            print(f"  {left}/{right}: {value:.2f}")
    return 0


def _read_all(paths: list[Path]) -> list[Bar]:
    bars = [bar for path in paths for bar in read_bars(path)]
    bars.sort(key=lambda b: (b.ts, b.instrument))
    return bars


def _baseline_params(args: argparse.Namespace, per_year: int) -> dict[str, object]:
    return {
        "trend": args.trend,
        "vol_window": args.vol_window,
        "target_vol": args.target_vol,
        "bars_per_year": args.bars_per_year or per_year,
    }


def research_gate(args: argparse.Namespace) -> int:
    bars = [b for b in read_bars(args.bars) if b.instrument == args.instrument]
    per_year = bars_per_year(bars)
    try:
        candidate = read_signal(args.signal, bars)
        baseline = baseline_weights(bars, **_baseline_params(args, per_year))
        verdict = ResearchGate(bars_per_year=per_year).evaluate(bars, candidate, baseline)
    except SignalError as exc:
        print(f"signal refused: {exc}", file=sys.stderr)
        return 2
    print_comparison(
        {
            "candidate": verdict.candidate,
            "baseline": verdict.baseline,
            "candidate (2nd half)": verdict.candidate_oos,
            "baseline (2nd half)": verdict.baseline_oos,
        }
    )
    if verdict.accepted:
        print("accepted: eligible for paper trading")
        return 0
    for reason in verdict.reasons:
        print(f"rejected: {reason}")
    return 1


def print_comparison(rows: dict[str, Performance]) -> None:
    def pct(value: float | None) -> str:
        return "-" if value is None else f"{value:.2%}"

    print(f"{'':18} {'return':>9} {'CAGR':>8} {'vol':>8} {'Sharpe':>7} {'max DD':>8}")
    for name, p in rows.items():
        sharpe = "-" if p.sharpe is None else f"{p.sharpe:.2f}"
        print(
            f"{name:18} {pct(p.total_return):>9} {pct(p.cagr):>8} {pct(p.volatility):>8} "
            f"{sharpe:>7} {pct(p.max_drawdown):>8}"
        )


def live(args: argparse.Namespace) -> int:
    def wall() -> datetime:
        return datetime.now(UTC)

    if args.wait_for_release:
        switch = KillSwitch(args.data_dir / "killswitch")
        if switch.engaged():
            print("kill switch engaged; waiting for a human to release it", flush=True)
        while switch.engaged():
            try:
                time.sleep(args.poll_s)
            except KeyboardInterrupt:
                return 3
    built = _executor(args, wall, persistent=True)
    if built is None:
        return 3
    executor, strategy = built
    source = McpBarSource(args.mcp_url)
    feeds = [
        LiveFeed(
            source=source,
            instrument=instrument,
            spread_bps=args.spread_bps,
            poll_s=args.poll_s,
            clock=wall,
            on_error=lambda msg: print(f"market data: {msg}", file=sys.stderr, flush=True),
            record=BarRecorder(args.data_dir / "bars"),
            max_polls=1 if args.once else None,
        )
        for instrument in args.instruments
    ]
    feed = BasketFeed(feeds, poll_s=args.poll_s, max_polls=1 if args.once else None)
    history = feed.warmup()
    if history:
        # Prime the strategy's averages; orders on these old bars would only fail the
        # quote-age check, so their intents are dropped.
        account = executor.account_view(wall())
        for bar in sorted(history, key=lambda b: (b.ts, b.instrument)):
            strategy.on_bar(bar, account)
    for f in feeds:
        primed = sum(1 for b in history if b.instrument == f.instrument)
        print(f"{f.instrument}: {primed} bars of history, polling every {args.poll_s:g} s")
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
    args: argparse.Namespace,
    clock: Callable[[], datetime],
    *,
    persistent: bool,
    bar_frequency: int = LIVE_BARS_PER_YEAR,
) -> tuple[OrderExecutor, Strategy] | None:
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
    # The order rate limit is per account, and a basket's members trade on the same minute,
    # so they share it: with three orders a minute, three members get one each.
    per_member = limits.max_orders_per_minute // len(args.instruments)
    if per_member < 1:
        print(
            f"a basket of {len(args.instruments)} exceeds {limits.max_orders_per_minute} "
            "orders a minute",
            file=sys.stderr,
        )
        return None
    members: dict[str, Strategy] = {}
    for instrument in args.instruments:
        if instrument not in limits.instruments:
            # The risk engine would refuse every order; say so once instead of on every bar.
            print(f"{instrument}: not on the risk allowlist, no orders", file=sys.stderr)
            continue
        if args.strategy == "trend-vol":
            members[instrument] = TrendVolTarget(
                instrument,
                trend=args.trend,
                vol_window=args.vol_window,
                target_vol=args.target_vol,
                capital=args.capital,
                max_order_notional=min(args.capital, limits.max_order_notional),
                max_orders_per_bar=per_member,
                quantity_step=args.step,
                bars_per_year=args.bars_per_year or bar_frequency,
            )
        else:
            members[instrument] = SmaCrossover(
                instrument,
                fast=args.fast,
                slow=args.slow,
                notional=args.notional,
                quantity_step=args.step,
            )
    if not members:
        print("no instrument on the risk allowlist", file=sys.stderr)
        return None
    strategy: Strategy = next(iter(members.values())) if len(members) == 1 else Basket(members)
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
            run.add_argument(
                "--wait-for-release",
                action="store_true",
                help="while the kill switch is engaged, wait instead of exiting (service mode)",
            )
        else:
            run.add_argument("--bars", type=Path, nargs="+", required=True)
        run.add_argument(
            "--instrument",
            required=True,
            help="one instrument, or a comma-separated basket such as SPY,QQQ",
        )
        run.add_argument("--strategy", choices=["sma", "trend-vol"], default="sma")
        run.add_argument("--trend", type=int, default=200, help="trend-vol: average length")
        run.add_argument("--vol-window", type=int, default=20, help="trend-vol: returns")
        run.add_argument("--target-vol", type=float, default=0.10, help="trend-vol: annual")
        run.add_argument("--capital", type=Decimal, default=Decimal(2400), help="trend-vol")
        run.add_argument(
            "--bars-per-year",
            type=int,
            default=None,
            help="trend-vol: annualisation; inferred from the bars, 98280 for paper-live",
        )
        if name == "backtest":
            run.add_argument(
                "--compare", action="store_true", help="compare with buy-and-hold of --capital"
            )
            run.add_argument(
                "--sweep",
                action="store_true",
                help="trend-vol over a grid of --trend and --target-vol, with buy-and-hold",
            )
        run.add_argument("--fast", type=int, default=10)
        run.add_argument("--slow", type=int, default=30)
        run.add_argument("--notional", type=Decimal, default=Decimal(1000))
        run.add_argument("--step", type=Decimal, default=Decimal(1))
        run.add_argument("--cash", type=Decimal, default=Decimal(10_000))
        run.add_argument("--spread-bps", type=Decimal, default=Decimal(4))
    gate = sub.add_parser("research-gate", help="does a signal beat the trend-vol baseline?")
    gate.add_argument("--bars", type=Path, required=True)
    gate.add_argument("--signal", type=Path, required=True, help="CSV: ts,weight per bar")
    gate.add_argument("--instrument", required=True)
    gate.add_argument("--trend", type=int, default=200)
    gate.add_argument("--vol-window", type=int, default=20)
    gate.add_argument("--target-vol", type=float, default=0.10)
    gate.add_argument("--bars-per-year", type=int, default=None)
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
    if args.command in ("backtest", "paper", "paper-live"):
        try:
            args.instruments = split_instruments(args.instrument)
        except PortfolioError as exc:
            parser.error(str(exc))

    if args.command == "backtest":
        return session(args, persistent=False)
    if args.command == "paper":
        return session(args, persistent=True)
    if args.command == "paper-live":
        return live(args)
    if args.command == "history":
        return history_csv(args)
    if args.command == "research-gate":
        return research_gate(args)
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
