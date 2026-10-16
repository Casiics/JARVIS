"""A paper-trading or backtest session: replay quotes, mark to market, ask the strategy,
execute through the executor. Backtests and paper trading share this path, so a strategy is
tested against the same gate, risk engine and kill switch it will trade under.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from jarvis_core.contracts.task_token import TaskTokenClaims
from mod_trading.executor import ExecutionResult, OrderExecutor
from mod_trading.schemas import Bar, Quote
from mod_trading.strategies import Strategy


@dataclass
class SessionReport:
    bars: int = 0
    intents: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)
    violations: Counter[str] = field(default_factory=Counter)
    kill_switch_trips: int = 0
    start_equity: Decimal | None = None
    end_equity: Decimal | None = None
    max_drawdown: Decimal = Decimal(0)
    results: list[ExecutionResult] = field(default_factory=list)
    equity: list[tuple[datetime, Decimal]] = field(default_factory=list)


def run_session(
    stream: Iterable[tuple[Bar, Quote]],
    strategy: Strategy,
    executor: OrderExecutor,
    claims: Callable[[], TaskTokenClaims],
    set_time: Callable[[datetime], None],
) -> SessionReport:
    report = SessionReport()
    executor.note(f"session:start:{strategy.name}")
    peak: Decimal | None = None
    for bar, quote in stream:
        set_time(quote.ts)
        was_engaged = executor.kill_switch.engaged()
        executor.broker.update_quote(quote)
        executor.mark_to_market()
        account = executor.account_view(quote.ts)
        report.bars += 1
        report.start_equity = report.start_equity or account.equity
        report.end_equity = account.equity
        report.equity.append((quote.ts, account.equity))
        peak = max(peak or account.equity, account.equity)
        report.max_drawdown = max(report.max_drawdown, (peak - account.equity) / peak)
        for intent in strategy.on_bar(bar, account):
            report.intents += 1
            result = executor.submit(intent, claims())
            report.outcomes[result.outcome.value] += 1
            report.violations.update(v.code.value for v in result.violations)
            report.results.append(result)
        if executor.kill_switch.engaged() and not was_engaged:
            report.kill_switch_trips += 1
    executor.note(f"session:end:{strategy.name}:{report.bars}")
    return report
