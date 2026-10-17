#!/usr/bin/env bash
# Starts the command deck on http://localhost:8080 for a local trial, without SOPS, Postgres
# or a tailnet. Creates throwaway secrets under JARVIS_DECK_LOCAL_DIR (default
# ~/.local/share/jarvis-deck-local), builds the deck, and prints the one-time setup code.
# Never use this on the server: the origin is plain http and the audit log is a local file
# chain (<state>/data/audit.jsonl), not pg-audit.
#
# Requires: uv, pnpm, openssl. Passkeys need a browser with a platform authenticator
# (Touch ID, Windows Hello) or a security key.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="${JARVIS_DECK_LOCAL_DIR:-$HOME/.local/share/jarvis-deck-local}"
PORT="${JARVIS_DECK_LOCAL_PORT:-8080}"

for tool in uv pnpm openssl; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done

umask 077
mkdir -p "$STATE/secrets/core" "$STATE/data"
for key in approval_grant_key deck_bootstrap_code task_token_signing_key; do
  [[ -s "$STATE/secrets/core/$key" ]] || openssl rand -hex 32 > "$STATE/secrets/core/$key"
done

cd "$REPO_ROOT"
uv sync --quiet
pnpm install --frozen-lockfile --silent
pnpm turbo run build --filter=@jarvis/deck >/dev/null

cat <<EOF

  Deck:           http://localhost:$PORT
  First start:    "Erster Start: Passkey einrichten", setup code:
                  $(cat "$STATE/secrets/core/deck_bootstrap_code")
  Local state:    $STATE (delete it to start over)
  Reels:          submitted reels wait in $STATE/boundary. To process them, start the
                  reel worker in a second terminal (setup: docs/runbooks/reel-pipeline.md):
                  scripts/reel-worker-local.sh

EOF

exec uv run python -m jarvis_core.api \
  --rp-id localhost --origin "http://localhost:$PORT" --port "$PORT" \
  --static apps/deck/dist --secrets-root "$STATE/secrets" --data-dir "$STATE/data" \
  --boundary-dir "$STATE/boundary"
