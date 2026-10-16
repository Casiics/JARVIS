# Runbook: trading domain, paper only (Phase 6)

Phase 6 builds the path every order will take, and runs it with paper money only. No broker account is connected, no broker key exists, and the executor refuses any broker that is not the paper broker. Design: [`architecture.md`](../architecture.md) sections 3.2 and 6.3, decision 10 in [`decisions.md`](../decisions.md). Code: `modules/mod-trading/`.

## The order path

```
strategy (plain code) ──OrderIntent──▶ action gate ──▶ risk engine + kill switch ──▶ paper broker
                                        (task token)     (modules/mod-trading/risk/)
```

1. A strategy proposes an `OrderIntent`. It cannot place anything itself.
2. The action gate decides an `ActionRequest` for `trading.place_order` against the task token. Paper orders are tier B. A task that saw external content (taint) is refused without a human approval, so an idea from a reel can never become an order on its own.
3. The risk engine (`trading_risk`, no model, no I/O) checks the order against the limits in `modules/mod-trading/risk/limits.json`:
   - instrument allowlist;
   - order, position and gross exposure notional;
   - no short selling;
   - price deviation from the quote and quote age;
   - orders per minute and per day;
   - daily loss and drawdown.
   
   It reports every violation, not just the first.
4. The kill switch is checked with the risk engine and again right before sending.
5. After every price update the executor marks the account to market. A daily loss or drawdown breach engages the kill switch and cancels all open orders. From then on nothing trades, not even a sell to cut the loss, until a human releases the switch.

Every step writes an audit entry with hashes and references, never order reasons or other content. Backtests and paper sessions use the same path, so a strategy is tested against the limits it will trade under.

## Kill switch

A single file, `killswitch` in the data directory. It fails closed: a missing, unreadable or unexpected file means engaged. Only the exact content `released` releases it. A fresh install therefore starts engaged.

```bash
python -m mod_trading kill-switch status
python -m mod_trading kill-switch engage "going on holiday"
python -m mod_trading kill-switch release      # a human step; no agent has a path to it
```

## Run

```bash
# Backtest: fresh paper account, switch released in memory, nothing persisted.
uv run python -m mod_trading backtest --bars spy-1m.csv --instrument SPY --fast 10 --slow 30 --notional 900

# Paper account: state, tracker, kill switch and audit chain in --data-dir (default /var/lib/jarvis/trading).
uv run python -m mod_trading --data-dir ~/jarvis-trading kill-switch release
uv run python -m mod_trading --data-dir ~/jarvis-trading paper --bars today.csv --instrument SPY
uv run python -m mod_trading --data-dir ~/jarvis-trading status
```

Bars are CSV with `ts` (ISO 8601 with offset, the bar's close), `instrument`, `open`, `high`, `low`, `close` and optionally `volume`. Each bar becomes a quote around its close (`--spread-bps`). The paper broker fills market orders at the far side plus slippage (5 bps) and charges a fee (10 bps). It rejects orders the cash or position cannot cover.

Limits: edit `modules/mod-trading/risk/limits.json`. It is a protected path, so every change goes through a PR with your review. Loading refuses inconsistent limits, such as an order limit above the position limit.

## Inbox

Ideas from the content domain (`TradingIdeaProposed`) enter through the boundary policy and keep their taint (`mod_trading.inbox`). `python -m mod_trading ideas` lists them. They are notes for review and have no path to the executor.

## Baseline strategy: trend filter plus volatility target

`--strategy trend-vol` (`mod_trading.strategies.TrendVolTarget`) is the rule-based baseline every later strategy has to beat. Long only, one instrument:

- **Trend filter:** invested only while the close is above its 200-bar average (`--trend`).
- **Volatility target:** while invested, the position is `capital × min(1, target_vol / realised_vol)`, with realised volatility from the last 20 returns (`--vol-window`), annualised, and `--target-vol 0.10`. Calm markets get the full `--capital`, turbulent ones less.
- **Trading:** rebalances only when the gap exceeds 10 % of the capital, and splits orders to stay under the risk limits' order size and per-minute count. `--capital` defaults to 2400, just under the 2500 position limit, because fills happen at the ask.

`backtest --compare` prints both curves on the same account (same starting cash), the strategy and buying `--capital` worth on the first bar and holding it:

```bash
uv run python -m mod_trading backtest --bars spy-daily.csv --instrument SPY --strategy trend-vol --compare
```

```
                      return     CAGR      vol  Sharpe   max DD
trend_vol_target       6.96%    3.16%    1.65%    1.31    2.03%
buy_and_hold          10.96%    4.93%    5.81%    0.60   11.18%
```

(Synthetic data from `tests/trading/test_trend_vol.py`: three years with a crash. Figures are on the whole account, so with 2400 of 10 000 invested they are smaller than the instrument's own.) Bar frequency is inferred: 252 bars a year for daily bars, more for intraday. Expect the strategy to lag buy-and-hold in steady bull markets and to win on drawdown in crashes; that trade-off is the point of a baseline. It is paper only: the executor refuses any broker but the paper broker.

`--sweep` runs the same backtest over trend lengths 100 to 250 and volatility targets 8 to 15 %, marks the current settings with `*` and runs that hit a risk limit with `!`. Use it as a robustness check: a setting is only credible when its neighbours do about as well, not because it is the best row.

Daily bars: any CSV in the format above works, for example an export of SPY's daily history. The live feed records 1-minute bars under `<data-dir>/bars/` for intraday backtests.

### On real data

S&P 500 monthly closes 1990-01 to 2026-09 (price index without dividends, from the public dataset `datasets/s-and-p-500` on GitHub), `--trend 10 --vol-window 6 --step 0.001 --compare`, so roughly the 200-day rule on monthly bars:

```
signal on the instrument (compounding, no account limits):
                      return     CAGR      vol  Sharpe   max DD
trend_vol_target    1428.05%    7.72%    8.03%    0.97   19.23%
buy_and_hold        2152.07%    8.87%   12.26%    0.76   50.82%
```

About one percentage point of yearly return given up for less than half of the worst loss and a better Sharpe ratio. On the paper account the same run stays far smaller (CAGR 1.5 %), because the risk limits cap the position at 2500 and the strategy keeps its capital fixed instead of compounding; that is intended until the limits are raised by a human. Daily SPY data from `history` will replace the monthly proxy.

## Research gate

Ideas from research agents (market researcher, earnings reviewer) are signals: a weight between 0 and 1 per bar, written as CSV `ts,weight`. Before a signal may trade on paper, `research-gate` compares it with the trend-vol baseline on the same bars, both as compounding weight curves with the same costs:

```bash
python -m mod_trading research-gate --bars spy-daily.csv --signal idea.csv --instrument SPY
```

It is accepted only if its Sharpe ratio beats the baseline's by at least 0.10 on the whole period and on the second half (out of sample), and its drawdown is at most 5 points worse. Exit code 0 means accepted, 1 rejected (with reasons), 2 an invalid signal (weights outside 0 to 1, bars without a weight). Buying and holding fails it, and so does the baseline itself; deterministic code decides, no model. An accepted signal still trades only through the normal order path.

## Live market data

`paper-live` trades the same paper account on live 1-minute bars instead of a CSV:

```
mod_trading (net-trading) ──MCP──▶ mcp-gateway-trading ──▶ n8n-trading ──▶ egress-trading ──▶ query1.finance.yahoo.com
                                   (OPA: endpoint marketdata,  workflow       (trading.allow)
                                    tool get_bars only)        get_bars
```

- **Provider:** Yahoo Finance's chart endpoint. It needs no account and no key and covers SPY, QQQ, IWM and most listed symbols, but it is unofficial: it can change or rate-limit without notice. The parser is the only provider-specific code (`mod_trading.marketdata.parse_chart`), so a keyed provider can replace it later.
- **Who calls out:** only n8n-trading, through egress-trading. `policies/egress/trading.allow` holds exactly the hosts the committed trading workflows call (`tests/config/test_policies.py` checks both directions).
- **What is checked:** the answer is untrusted. It is size-capped and parsed with strict models. One inconsistent bar (high below low, wrong symbol or bar size, columns of different length, out of order) refuses the whole answer, and n8n's error text is never passed on. The bar that is still forming is dropped.
- **Time:** live sessions run on the wall clock. A bar's quote is as old as its close, so data that stops arriving fails the risk engine's quote-age limit (`max_quote_age_s`) instead of trading on old prices.
- **Start of a session:** the first poll returns today's bars so far. They prime the strategy's averages; intents on them are dropped. After that, each new closed bar goes through the normal order path.
- **Recording:** every traded bar is appended to `<data-dir>/bars/<instrument>-<date>.csv`, which `backtest --bars` reads directly.
- **History for backtests:** `python -m mod_trading history --instrument SPY --out spy-daily.csv` asks the second tool, `get_daily`, for up to ten years of daily bars and writes them as backtest CSV.

### One-time setup

```bash
# 1. Import the workflow into n8n-trading and publish it (it is committed inactive).
docker compose -f docker/docker-compose.yml run --rm \
  -v "$PWD/core/n8n-workflows/trading-marketdata.json:/wf.json:ro" --entrypoint n8n n8n-trading import:workflow --input=/wf.json
docker compose -f docker/docker-compose.yml run --rm --entrypoint n8n n8n-trading publish:workflow --id=jarvisTradingMd01
# 2. Load the new allowlist and tool list.
docker compose -f docker/docker-compose.yml up -d n8n-trading opa-trading mcp-gateway-trading egress-trading
docker compose -f docker/docker-compose.yml restart opa-trading egress-trading
```

### Run

`paper-live` must run where `mcp-gateway-trading` resolves, which is `net-trading`. It polls every 60 s (`--poll-s`); `--once` polls a single time, which is the quickest check that data arrives.

```bash
python -m mod_trading --data-dir /var/lib/jarvis/trading kill-switch release
python -m mod_trading --data-dir /var/lib/jarvis/trading paper-live --instrument SPY
```

Messages starting with `market data:` mean a poll brought nothing usable; the session keeps polling and trades nothing on that poll. `tests/isolation/test_trading_marketdata_live.py` (`JARVIS_TEST_DOCKER=1`) runs the committed workflow in a real n8n behind the gateway.

## Exit criterion and what is missing for it

The roadmap asks for several weeks of paper trading without a risk-limit violation before a live key exists. The path is complete and tested (`tests/trading/`), and live bars arrive through n8n-trading. For a continuous paper run it still needs:

- **A service.** A `trading-paper` container on `net-trading` (no egress) that runs `paper-live` and restarts with the stack. It changes `docker/`, a protected path.
- **Deck integration.** The kill switch and daily P&L in the deck's top bar arrive with the orchestrator service that connects domains to the API gateway.
