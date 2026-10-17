#!/usr/bin/env bash
# One command for the whole local trial on a workstation (WSL2 + Docker Desktop works):
#
#   scripts/jarvis-local.sh setup    once: check tools and GPU, pull the local models,
#                                    download Whisper, build the reel sandbox images
#   scripts/jarvis-local.sh up       start Ollama, the reel download proxy, the deck with its
#                                    orchestrator, the reel worker and context curation
#   scripts/jarvis-local.sh e2e      end-to-end test against the running stack
#   scripts/jarvis-local.sh status   what runs, where the logs are
#   scripts/jarvis-local.sh logs [deck|reel|curation]
#   scripts/jarvis-local.sh down     stop everything `up` started
#
# Local trial only: plain http on localhost, throwaway deck secrets, file audit chains, Ollama
# published on 127.0.0.1 (docker-compose.debug.yml). Never use it on the server.
#
# Settings via environment:
#   JARVIS_DECK_LOCAL_DIR  deck state (default ~/.local/share/jarvis-deck-local)
#   JARVIS_DATA            content data and models (default ~/jarvis-data)
#   JARVIS_DECK_LOCAL_PORT deck port (default 8080)
#   JARVIS_NO_GPU=1        run Ollama on the CPU (slow; for machines without an NVIDIA GPU)
#   JARVIS_WITH_HERMES=1   setup also pulls hermes3:8b for the Hermes runtime
#   JARVIS_E2E_REEL=<url>  e2e also runs one real reel through the pipeline
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="${JARVIS_DECK_LOCAL_DIR:-$HOME/.local/share/jarvis-deck-local}"
DATA="${JARVIS_DATA:-$HOME/jarvis-data}"
PORT="${JARVIS_DECK_LOCAL_PORT:-8080}"
RUN="$STATE/run"
LOGS="$STATE/logs"
OLLAMA_URL="http://127.0.0.1:11434"
MODELS=(qwen2.5vl:7b deepseek-r1:8b)
WHISPER="$DATA/models/whisper/large-v3"
COMPOSE_FILES=(-f "$REPO_ROOT/docker/docker-compose.yml")

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

need() {
  for tool in "$@"; do
    command -v "$tool" >/dev/null || die "missing tool: $tool (see the start guide, part A)"
  done
}

need_docker() {
  need docker
  docker info >/dev/null 2>&1 || die "docker is not running: start Docker Desktop and enable
       Settings > Resources > WSL integration for this distro, then open a new terminal"
}

compose() {
  local files=("${COMPOSE_FILES[@]}")
  if [[ "${JARVIS_NO_GPU:-0}" == "1" ]]; then
    # Drops Ollama's GPU reservation; written outside the repo so docker/ stays untouched.
    mkdir -p "$RUN"
    printf 'services:\n  ollama:\n    deploy: !reset {}\n' > "$RUN/compose.no-gpu.yml"
    files+=(-f "$RUN/compose.no-gpu.yml")
  fi
  docker compose "${files[@]}" "$@"
}

# Runs docker compose quietly; on failure shows the tail of its output.
compose_q() {
  mkdir -p "$LOGS"
  compose "$@" >"$LOGS/compose.log" 2>&1 || { tail -n 20 "$LOGS/compose.log" >&2; exit 1; }
}

ollama_up() {
  compose_q -f "$REPO_ROOT/docker/docker-compose.debug.yml" up -d --wait ollama
}

ollama_has() {
  curl -fsS "$OLLAMA_URL/api/tags" 2>/dev/null | grep -q "\"name\":\"$1\""
}

pid_alive() { [[ -f "$RUN/$1.pid" ]] && kill -0 "$(cat "$RUN/$1.pid")" 2>/dev/null; }

start_bg() {
  local name="$1"
  shift
  if pid_alive "$name"; then
    echo "  $name already running (pid $(cat "$RUN/$name.pid"))"
    return
  fi
  # setsid: own process group, so `down` stops the process and everything it started.
  setsid nohup "$@" >"$LOGS/$name.log" 2>&1 </dev/null &
  echo $! >"$RUN/$name.pid"
  echo "  $name started, log: $LOGS/$name.log"
}

stop_bg() {
  local name="$1"
  if pid_alive "$name"; then
    kill -TERM -- "-$(cat "$RUN/$name.pid")" 2>/dev/null || true
    echo "  $name stopped"
  fi
  rm -f "$RUN/$name.pid"
}

cmd_setup() {
  need uv pnpm openssl curl
  need_docker
  say "1/5 GPU"
  if [[ "${JARVIS_NO_GPU:-0}" == "1" ]]; then
    echo "  skipped (JARVIS_NO_GPU=1, Ollama runs on the CPU)"
  elif docker run --rm --gpus all ubuntu:24.04 nvidia-smi -L 2>/dev/null; then
    :
  else
    die "Docker sees no NVIDIA GPU. Update the Windows NVIDIA driver and restart Docker
       Desktop, or set JARVIS_NO_GPU=1 to run on the CPU."
  fi

  say "2/5 Python and deck dependencies"
  (cd "$REPO_ROOT" && uv sync --quiet && pnpm install --frozen-lockfile --silent)

  say "3/5 Local models (Ollama gets internet only for this step)"
  local models=("${MODELS[@]}")
  [[ "${JARVIS_WITH_HERMES:-0}" == "1" ]] && models+=(hermes3:8b)
  compose_q -f "$REPO_ROOT/docker/docker-compose.models.yml" up -d --wait ollama
  for model in "${models[@]}"; do
    echo "  pulling $model"
    compose exec -T ollama ollama pull "$model" >/dev/null
  done
  # Back to the offline network set; the debug override publishes 127.0.0.1:11434.
  ollama_up

  say "4/5 Whisper model"
  if [[ -f "$WHISPER/model.bin" ]]; then
    echo "  already in $WHISPER"
  else
    mkdir -p "$WHISPER" "$DATA/content"
    uvx --from huggingface_hub hf download Systran/faster-whisper-large-v3 --local-dir "$WHISPER"
  fi

  say "5/5 Reel sandbox images"
  local sandbox="$REPO_ROOT/modules/mod-content/sandbox"
  docker build -q -t localhost/jarvis-reel-media:0.1.0 -f "$sandbox/Dockerfile.media" "$sandbox" >/dev/null
  docker build -q -t localhost/jarvis-reel-whisper:0.1.0 -f "$sandbox/Dockerfile.whisper" "$sandbox" >/dev/null
  say "Setup done. Next: scripts/jarvis-local.sh up"
}

cmd_up() {
  need uv pnpm openssl curl
  need_docker
  mkdir -p "$RUN" "$LOGS" "$DATA/content"

  say "Ollama"
  ollama_up
  for model in "${MODELS[@]}"; do
    ollama_has "$model" || echo "  warning: $model missing; run scripts/jarvis-local.sh setup"
  done

  say "Deck with orchestrator"
  JARVIS_DECK_LOCAL_DIR="$STATE" JARVIS_DECK_LOCAL_PORT="$PORT" \
    start_bg deck "$REPO_ROOT/scripts/deck-local.sh"
  local ok=0
  for _ in $(seq 1 600); do
    if curl -fsS "http://localhost:$PORT/healthz" >/dev/null 2>&1; then ok=1; break; fi
    pid_alive deck || break
    sleep 1
  done
  [[ $ok == 1 ]] || die "the deck did not start; see $LOGS/deck.log"

  say "Reel worker"
  if [[ -f "$WHISPER/model.bin" ]]; then
    JARVIS_DECK_LOCAL_DIR="$STATE" JARVIS_DATA="$DATA" \
      start_bg reel "$REPO_ROOT/scripts/reel-worker-local.sh"
  else
    echo "  skipped: no Whisper model yet (scripts/jarvis-local.sh setup)"
  fi

  say "Context curation"
  start_bg curation bash -c "cd '$REPO_ROOT' && exec uv run python -m mod_content.curation \
    --data-dir '$DATA/content' run --watch --boundary-dir '$STATE/boundary'"

  cat <<EOF

  Deck:        http://localhost:$PORT   (open it in the Windows browser)
  First start: "Erster Start: Passkey einrichten", setup code:
               $(cat "$STATE/secrets/core/deck_bootstrap_code")
  Test:        scripts/jarvis-local.sh e2e
  Stop:        scripts/jarvis-local.sh down

EOF
}

cmd_down() {
  say "Stopping"
  for name in curation reel deck; do stop_bg "$name"; done
  if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
    compose stop ollama egress-reel >/dev/null 2>&1 || true
    echo "  ollama and egress-reel stopped"
  fi
}

cmd_status() {
  for name in deck reel curation; do
    if pid_alive "$name"; then
      echo "  $name: running (log $LOGS/$name.log)"
    else
      echo "  $name: stopped"
    fi
  done
  if curl -fsS "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
    echo "  ollama: running on $OLLAMA_URL"
  else
    echo "  ollama: not reachable"
  fi
  if [[ -d "$STATE/data" ]]; then
    (cd "$REPO_ROOT" && uv run --quiet python -m jarvis_core.orchestrator \
      --data-dir "$STATE/data" status) || true
  fi
}

cmd_logs() {
  local name="${1:-deck}"
  [[ -f "$LOGS/$name.log" ]] || die "no log for $name in $LOGS"
  tail -n 100 -f "$LOGS/$name.log"
}

# --- end-to-end test ---------------------------------------------------------------------------

PASSED=0
FAILED=0
SKIPPED=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASSED=$((PASSED + 1)); }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILED=$((FAILED + 1)); }
skip() { printf '  \033[33mSKIP\033[0m %s\n' "$*"; SKIPPED=$((SKIPPED + 1)); }

py() { (cd "$REPO_ROOT" && uv run --quiet python "$@"); }

cmd_e2e() {
  need uv curl
  local base="http://localhost:$PORT"
  local work="$STATE/e2e"
  rm -rf "$work"
  mkdir -p "$work"
  say "End-to-end test (scratch data in $work)"

  # 1. Deck and gateway
  if curl -fsS "$base/healthz" 2>/dev/null | grep -q '"ok"'; then
    pass "deck answers on $base"
  else
    fail "deck not reachable on $base (scripts/jarvis-local.sh up)"
  fi
  if curl -fsS "$base/" 2>/dev/null | grep -qi '<!doctype html>'; then
    pass "deck page is served"
  else
    fail "deck page missing (build failed? see $LOGS/deck.log)"
  fi
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' "$base/api/tasks" || true)"
  if [[ "$code" == "401" || "$code" == "403" ]]; then
    pass "API refuses requests without a passkey session ($code)"
  else
    fail "API answered $code without a session; expected 401 or 403"
  fi

  # 2. Audit chain and orchestrator
  if [[ -f "$STATE/data/audit.jsonl" ]]; then
    if py -c "import sys; from pathlib import Path; from jarvis_core.audit.file import FileAuditStore; \
print(len(FileAuditStore(Path(sys.argv[1])).entries()))" "$STATE/data/audit.jsonl" \
      >"$work/audit.out" 2>&1; then
      pass "deck audit chain intact ($(cat "$work/audit.out") entries)"
    else
      fail "deck audit chain broken: $(tail -1 "$work/audit.out")"
    fi
  else
    skip "no deck audit log yet (start the deck once)"
  fi
  if py -m jarvis_core.orchestrator --data-dir "$STATE/data" status >"$work/orch.out" 2>&1; then
    pass "orchestrator status readable"
  else
    fail "orchestrator status: $(tail -1 "$work/orch.out")"
  fi

  # 3. Context pipeline: collect, consolidate, prepare, decide (notes inbox, no models needed)
  local cur="$work/content"
  mkdir -p "$cur/curation/inbox"
  cat >"$cur/curation/inbox/e2e.md" <<'NOTE'
JARVIS end-to-end check
signal: 0.9
A note that proves the four curation stages run. #cli
- Drop a note into the inbox
- Run one curation pass
NOTE
  # A fresh note lands in review (confidence between the category's thresholds), so this
  # also exercises stage 4 the way a human answers it.
  local brief=""
  if py -m mod_content.curation --data-dir "$cur" run >"$work/curation.out" 2>&1; then
    py -m mod_content.curation --data-dir "$cur" review >"$work/review.out" 2>&1 || true
    brief="$(grep -o '^br:[0-9a-f]*' "$work/review.out" | head -1 || true)"
    if [[ -n "$brief" ]]; then
      py -m mod_content.curation --data-dir "$cur" decide "$brief" accept \
        >>"$work/review.out" 2>&1 || true
    fi
  fi
  if py -m mod_content.curation --data-dir "$cur" context >"$work/context.out" 2>&1 \
    && grep -q "JARVIS end-to-end check" "$work/context.out"; then
    pass "context pipeline: collected, consolidated, reviewed${brief:+ ($brief)} and stored"
  else
    fail "context pipeline: see $work/curation.out, review.out and context.out"
  fi

  # 4. Trading on paper: trend-vol baseline vs buy-and-hold on synthetic daily bars
  py - "$work/bars.csv" <<'PY'
import math, random, sys
from datetime import datetime, timedelta, timezone
random.seed(7)
price, ts = 400.0, datetime(2022, 1, 3, 21, tzinfo=timezone.utc)
with open(sys.argv[1], "w") as out:
    out.write("ts,instrument,open,high,low,close,volume\n")
    for i in range(600):
        drift = -0.004 if 300 <= i < 340 else 0.0004
        close = price * math.exp(drift + random.gauss(0, 0.01))
        hi, lo = max(price, close) * 1.002, min(price, close) * 0.998
        out.write(f"{ts.isoformat()},SPY,{price:.2f},{hi:.2f},{lo:.2f},{close:.2f},1000000\n")
        price, ts = close, ts + timedelta(days=1)
PY
  if py -m mod_trading --data-dir "$work/trading" backtest --bars "$work/bars.csv" \
    --instrument SPY --strategy trend-vol --compare >"$work/trading.out" 2>&1; then
    pass "paper backtest ran through risk engine and kill switch"
  else
    fail "paper backtest: $(tail -1 "$work/trading.out")"
  fi

  # 5. Supervisor-worker loop on the local models
  if ! curl -fsS "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
    skip "model loop: Ollama not reachable on $OLLAMA_URL"
  elif ! ollama_has deepseek-r1:8b; then
    skip "model loop: deepseek-r1:8b not pulled (scripts/jarvis-local.sh setup)"
  elif py -m jarvis_core.agents smoke --ollama-url "$OLLAMA_URL" >"$work/smoke.out" 2>&1; then
    pass "supervisor-worker loop finished on local models"
  else
    fail "model loop: see $work/smoke.out"
  fi
  if [[ "${JARVIS_WITH_HERMES:-0}" == "1" ]]; then
    if ! command -v hermes >/dev/null; then
      skip "Hermes loop: hermes CLI not on PATH"
    elif py -m jarvis_core.agents smoke --runtime hermes --ollama-url "$OLLAMA_URL" \
      >"$work/hermes.out" 2>&1; then
      pass "supervisor-worker loop finished on Hermes"
    else
      fail "Hermes loop: see $work/hermes.out"
    fi
  fi

  # 6. One real reel (optional, needs internet for the download)
  if [[ -z "${JARVIS_E2E_REEL:-}" ]]; then
    skip "real reel: set JARVIS_E2E_REEL=<instagram reel link> to include one"
  elif [[ ! -f "$WHISPER/model.bin" ]]; then
    skip "real reel: no Whisper model (scripts/jarvis-local.sh setup)"
  elif py -m mod_content.reel --data-dir "$work/reel" ingest --engine docker \
    --whisper-models "$DATA/models/whisper" --show-sandbox-errors "$JARVIS_E2E_REEL" \
    >"$work/reel.out" 2>&1 && grep -Eq ': (done|awaiting_[a-z]+)' "$work/reel.out"; then
    pass "real reel processed"
  else
    fail "real reel: see $work/reel.out"
  fi

  echo
  say "Result: $PASSED passed, $FAILED failed, $SKIPPED skipped"
  [[ $FAILED == 0 ]]
}

case "${1:-}" in
  setup) cmd_setup ;;
  up) cmd_up ;;
  e2e) cmd_e2e ;;
  status) cmd_status ;;
  logs) cmd_logs "${2:-deck}" ;;
  down) cmd_down ;;
  *) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
