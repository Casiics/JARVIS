#!/usr/bin/env bash
# Creates one age key per domain and an encrypted secrets/<domain>.sops.yaml with random values.
# Run once on the server. Existing keys and secret files are never overwritten.
#
#   JARVIS_AGE_DIR   where private age keys live (default /etc/jarvis/age, root-owned, never in git)
#
# Requires: age, age-keygen, sops, openssl.
set -euo pipefail

AGE_DIR="${JARVIS_AGE_DIR:-/etc/jarvis/age}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SECRETS_DIR="$REPO_ROOT/secrets"

declare -A KEYS=(
  [core]="pg_audit_superuser_password pg_audit_writer_password pg_audit_reader_password pg_superuser_password pg_app_password pg_agent_ro_password redis_password task_token_signing_key approval_grant_key deck_bootstrap_code"
  [trading]="pg_superuser_password pg_app_password pg_agent_ro_password"
  [content]="pg_superuser_password pg_app_password pg_agent_ro_password qdrant_api_key"
  [private]="pg_superuser_password pg_app_password pg_agent_ro_password qdrant_api_key redis_password"
)
DOMAINS=(core trading content private)

for tool in age-keygen sops openssl; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done

umask 077
mkdir -p "$AGE_DIR" "$SECRETS_DIR"

rules=""
for domain in "${DOMAINS[@]}"; do
  key_file="$AGE_DIR/$domain.key"
  if [[ ! -f "$key_file" ]]; then
    age-keygen -o "$key_file" 2>/dev/null
    echo "created age key $key_file (store an offline copy now)"
  fi
  recipient="$(age-keygen -y "$key_file")"
  rules+="  - path_regex: secrets/${domain}\\.sops\\.yaml\$
    age: ${recipient}
"
done
printf 'creation_rules:\n%s' "$rules" > "$REPO_ROOT/.sops.yaml"

for domain in "${DOMAINS[@]}"; do
  target="$SECRETS_DIR/$domain.sops.yaml"
  if [[ -f "$target" ]]; then
    echo "keeping existing $target"
    continue
  fi
  plain="$(mktemp)"
  trap 'rm -f "$plain"' EXIT
  for key in ${KEYS[$domain]}; do
    printf '%s: "%s"\n' "$key" "$(openssl rand -hex 32)" >> "$plain"
  done
  (cd "$REPO_ROOT" && sops --encrypt --filename-override "secrets/$domain.sops.yaml" \
      --input-type yaml --output-type yaml "$plain" > "$target")
  rm -f "$plain"
  echo "wrote $target"
done
