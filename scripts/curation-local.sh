#!/usr/bin/env bash
# Runs content curation for the local deck trial (scripts/deck-local.sh) in a third terminal.
# Every finished reel (runs.jsonl) and every note dropped into the inbox starts a new pass within
# a few seconds; open reviews go to the deck and its answers come back through the same mailbox.
# A failing pass is reported as an error class and retried, the watcher keeps running.
#
# Settings via environment:
#   JARVIS_DECK_LOCAL_DIR   the deck's local state (default ~/.local/share/jarvis-deck-local)
#   JARVIS_DATA             content data, as for the reel worker (default ~/jarvis-data)
#   JARVIS_CURATION_POLL    seconds between checks (default 5; a check only stats the inputs)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="${JARVIS_DECK_LOCAL_DIR:-$HOME/.local/share/jarvis-deck-local}"
DATA="${JARVIS_DATA:-$HOME/jarvis-data}"
POLL="${JARVIS_CURATION_POLL:-5}"

command -v uv >/dev/null || { echo "missing tool: uv" >&2; exit 1; }
mkdir -p "$DATA/content/curation/inbox"

cat <<INFO

  Watching:   $DATA/content/runs.jsonl (finished reels)
              $DATA/content/curation/inbox/ (notes, .md or .txt)
  Reviews:    in the deck under Content, via $STATE/boundary
  Stop:       Ctrl+C

INFO

cd "$REPO_ROOT"
exec uv run python -m mod_content.curation --data-dir "$DATA/content" run --watch \
  --poll-seconds "$POLL" --boundary-dir "$STATE/boundary"
