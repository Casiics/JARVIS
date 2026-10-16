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

## Exit criterion and what is missing for it

The roadmap asks for several weeks of paper trading without a risk-limit violation before a live key exists. The path is complete and tested (`tests/trading/`). For a continuous paper run it still needs:

- **A market-data feed.** Right now paper sessions replay bars you provide. A live feed would come from `n8n-trading` polling a market-data API through `egress-trading`. That needs a provider and its host on `policies/egress/trading.allow`, which is the owner's choice.
- **A scheduler and service.** A `trading-paper` container on `net-trading` with no egress, started per session, follows with the feed.
- **Deck integration.** The kill switch and daily P&L in the deck's top bar arrive with the orchestrator service that connects domains to the API gateway.
