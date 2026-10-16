#!/usr/bin/env bash
# Starts the reel worker for the local deck trial (scripts/deck-local.sh), in a second terminal.
# Builds the sandbox images (cached, so quick after the first time, and always current after a
# git pull), starts the egress-reel download proxy, then runs the worker on the deck's mailbox
# with sandbox errors shown here.
#
# Requires: uv, docker with compose (Docker Desktop with WSL integration works), Ollama and the
# Whisper model as in docs/runbooks/reel-pipeline.md. Settings via environment:
#   JARVIS_DECK_LOCAL_DIR  the deck's local state (default ~/.local/share/jarvis-deck-local)
#   JARVIS_DATA            content data and models (default ~/jarvis-data)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="${JARVIS_DECK_LOCAL_DIR:-$HOME/.local/share/jarvis-deck-local}"
DATA="${JARVIS_DATA:-$HOME/jarvis-data}"
SANDBOX="$REPO_ROOT/modules/mod-content/sandbox"

for tool in uv docker; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done
docker info >/dev/null 2>&1 || { echo "docker is not running (try: docker info)" >&2; exit 1; }
[[ -d "$DATA/models/whisper" ]] || {
  echo "no Whisper model in $DATA/models/whisper (docs/runbooks/reel-pipeline.md, step 3)" >&2
  exit 1
}

echo "Building sandbox images (cached after the first run) ..."
docker build -q -t localhost/jarvis-reel-media:0.1.0 -f "$SANDBOX/Dockerfile.media" "$SANDBOX" >/dev/null
docker build -q -t localhost/jarvis-reel-whisper:0.1.0 -f "$SANDBOX/Dockerfile.whisper" "$SANDBOX" >/dev/null

echo "Starting the download proxy egress-reel ..."
docker compose -f "$REPO_ROOT/docker/docker-compose.yml" up -d --wait egress-reel >/dev/null

cd "$REPO_ROOT"
exec uv run python -m mod_content.reel --data-dir "$DATA/content" serve \
  --engine docker --whisper-models "$DATA/models/whisper" \
  --boundary-dir "$STATE/boundary" --show-sandbox-errors
