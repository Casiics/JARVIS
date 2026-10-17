#!/usr/bin/env bash
# Runs the paper-trading basket (SPY, QQQ, IWM) around the clock on a workstation or server:
#
#   scripts/trading-paper-local.sh setup     once: check secrets, start n8n-trading and its
#                                            gateway, import and publish the market-data
#                                            workflow, build the trading image
#   scripts/trading-paper-local.sh release   release the kill switch (asks you to type it)
#   scripts/trading-paper-local.sh up        start the paper session in the background
#   scripts/trading-paper-local.sh history   refresh ten years of daily bars per instrument and
#                                            copy them to $JARVIS_DATA/trading/history
#   scripts/trading-paper-local.sh research  one research-lab session on that history (local
#                                            models only; needs Ollama from jarvis-local.sh up)
#   scripts/trading-paper-local.sh status    account, positions, kill switch, P&L
#   scripts/trading-paper-local.sh logs      follow the session
#   scripts/trading-paper-local.sh down      stop the session (account and bars are kept)
#
# Paper only: the executor refuses every broker but the paper broker, and there is no broker key.
# The kill switch starts engaged on a fresh volume; nothing trades until a human releases it.
#
# Settings via environment:
#   JARVIS_PAPER_INSTRUMENT  basket (default SPY,QQQ,IWM; each must be on the risk allowlist)
#   JARVIS_PAPER_STRATEGY    strategy (default trend-vol)
#   JARVIS_DATA              host copy of history and the research ledger (default ~/jarvis-data)
#   JARVIS_SECRETS_DIR       unpacked secrets (default /run/jarvis/secrets, scripts/secrets-unpack.sh)
#   JARVIS_OLLAMA_URL        for research (default http://127.0.0.1:11434)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${JARVIS_DATA:-$HOME/jarvis-data}"
SECRETS="${JARVIS_SECRETS_DIR:-/run/jarvis/secrets}"
OLLAMA_URL="${JARVIS_OLLAMA_URL:-http://127.0.0.1:11434}"
export JARVIS_PAPER_INSTRUMENT="${JARVIS_PAPER_INSTRUMENT:-SPY,QQQ,IWM}"
export JARVIS_PAPER_STRATEGY="${JARVIS_PAPER_STRATEGY:-trend-vol}"
VOLUME_DIR=/var/lib/jarvis/trading
INFRA=(n8n-trading opa-trading mcp-gateway-trading egress-trading)

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

compose() { docker compose -f "$REPO_ROOT/docker/docker-compose.yml" --profile paper "$@"; }
# One-off command in the trading image against the session's volume.
trading() { compose run --rm -T trading-paper "$@"; }

need_docker() {
  command -v docker >/dev/null || die "missing tool: docker"
  docker info >/dev/null 2>&1 || die "docker is not running"
}

need_secrets() {
  [[ -r "$SECRETS/trading/n8n_encryption_key" ]] ||
    die "trading secrets are not unpacked: run scripts/secrets-unpack.sh trading
       (or set JARVIS_SECRETS_DIR to where they are)"
}

instruments() { tr ',' ' ' <<<"$JARVIS_PAPER_INSTRUMENT"; }

cmd_setup() {
  need_docker
  need_secrets
  say "Starting n8n-trading, its gateway, policy and egress proxy"
  compose up -d --wait "${INFRA[@]}"
  say "Importing and publishing the market-data workflow"
  compose run --rm -T -v "$REPO_ROOT/core/n8n-workflows/trading-marketdata.json:/wf.json:ro" \
    --entrypoint n8n n8n-trading import:workflow --input=/wf.json
  compose run --rm -T --entrypoint n8n n8n-trading publish:workflow --id=jarvisTradingMd01
  # n8n loads published workflows at start; opa and egress reload the tool list and allowlist.
  compose restart n8n-trading opa-trading egress-trading
  say "Building the trading image"
  compose build trading-paper
  say "Checking that market data arrives (outside US hours this may bring no bars)"
  trading paper-live --instrument "${JARVIS_PAPER_INSTRUMENT%%,*}" --once ||
    echo "  no bars on this check; see 'market data:' above. The session keeps polling."
  cat <<INFO

  Next:  scripts/trading-paper-local.sh release   (a human step)
         scripts/trading-paper-local.sh up

INFO
}

cmd_release() {
  need_docker
  trading kill-switch status || true
  printf 'Type "release" to let the paper account trade %s: ' "$JARVIS_PAPER_INSTRUMENT"
  read -r answer
  [[ "$answer" == "release" ]] || die "not released"
  trading kill-switch release
}

cmd_up() {
  need_docker
  need_secrets
  compose up -d --wait "${INFRA[@]}"
  compose up -d trading-paper
  say "Paper session running: $JARVIS_PAPER_INSTRUMENT, $JARVIS_PAPER_STRATEGY"
  echo "  It restarts with Docker and polls every 60 s; it trades only while the kill switch is"
  echo "  released and US markets are open. Status: $0 status   Logs: $0 logs"
}

cmd_history() {
  need_docker
  mkdir -p "$DATA/trading/history"
  for s in $(instruments); do
    local name
    name="$(tr '[:upper:]' '[:lower:]' <<<"$s")-daily.csv"
    say "Daily history for $s"
    trading history --instrument "$s" --out "$VOLUME_DIR/history/$name"
    compose run --rm -T --entrypoint cat trading-paper "$VOLUME_DIR/history/$name" \
      >"$DATA/trading/history/$name"
    echo "  $(($(wc -l <"$DATA/trading/history/$name") - 1)) bars -> $DATA/trading/history/$name"
  done
}

cmd_research() {
  command -v uv >/dev/null || die "missing tool: uv"
  [[ -f "$REPO_ROOT/modules/mod-trading/trading_agents/lab.py" ]] ||
    die "the research lab is not on this checkout yet (pull request 'trading research workers')"
  curl -fsS "$OLLAMA_URL/api/tags" >/dev/null 2>&1 ||
    die "no Ollama at $OLLAMA_URL: start it with scripts/jarvis-local.sh up"
  local bars=()
  for s in $(instruments); do
    bars+=("$DATA/trading/history/$(tr '[:upper:]' '[:lower:]' <<<"$s")-daily.csv")
  done
  for f in "${bars[@]}"; do [[ -s "$f" ]] || die "missing $f: run $0 history first"; done
  cd "$REPO_ROOT"
  uv run python -m trading_agents.lab --data-dir "$DATA/trading/lab" run \
    --bars "${bars[@]}" --ollama-url "$OLLAMA_URL"
  uv run python -m trading_agents.lab --data-dir "$DATA/trading/lab" ledger
}

case "${1:-}" in
  setup) cmd_setup ;;
  release) cmd_release ;;
  up) cmd_up ;;
  history) cmd_history ;;
  research) cmd_research ;;
  status) need_docker; trading status ;;
  logs) need_docker; compose logs -f trading-paper ;;
  down) need_docker; compose stop trading-paper ;;
  *) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
