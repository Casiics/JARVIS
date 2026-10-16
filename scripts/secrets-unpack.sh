#!/usr/bin/env bash
# Decrypts secrets/<domain>.sops.yaml into ${JARVIS_SECRETS_DIR}/<domain>/<key>, one file per key.
# Run before `docker compose up`. Point JARVIS_SECRETS_DIR at tmpfs (default /run is tmpfs).
#
# Directories are 0700 and root-owned, so no other host user can reach the files. The files
# themselves are 0444 because containers read them as their own non-root user (e.g. postgres).
#
#   scripts/secrets-unpack.sh            # all domains
#   scripts/secrets-unpack.sh trading    # one domain
set -euo pipefail

AGE_DIR="${JARVIS_AGE_DIR:-/etc/jarvis/age}"
OUT_DIR="${JARVIS_SECRETS_DIR:-/run/jarvis/secrets}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [[ $# -gt 0 ]]; then DOMAINS=("$@"); else DOMAINS=(core trading content private); fi

umask 077
for domain in "${DOMAINS[@]}"; do
  src="$REPO_ROOT/secrets/$domain.sops.yaml"
  dest="$OUT_DIR/$domain"
  mkdir -p "$dest"
  chmod 700 "$OUT_DIR" "$dest"
  SOPS_AGE_KEY_FILE="$AGE_DIR/$domain.key" sops --decrypt --output-type json "$src" |
    python3 -c '
import json, os, re, sys
dest = sys.argv[1]
for key, value in json.load(sys.stdin).items():
    if not re.fullmatch(r"[a-z0-9_]+", key):
        sys.exit(f"invalid secret key: {key}")
    path = os.path.join(dest, key)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o444)
    with os.fdopen(fd, "w") as fh:
        fh.write(str(value))
    os.chmod(path, 0o444)  # umask 077 would otherwise leave it 0400
' "$dest"
  echo "unpacked $domain -> $dest"
done
