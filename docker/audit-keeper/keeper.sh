#!/bin/sh
# Entry point of the audit-keeper container. The host runs `checkpoint` hourly and `export`
# daily (docs/runbooks/audit-hardening.md). Passwords and the signing key are compose secrets.
set -eu

writer="host=pg-audit dbname=jarvis user=audit_writer"
reader="host=pg-audit dbname=jarvis user=audit_reader"
run() { exec python -m jarvis_core.audit --domain core "$@"; }

command="${1:-}"
[ $# -gt 0 ] && shift
case "$command" in
  checkpoint)
    run checkpoint --dsn "$writer" --password-file /run/secrets/pg_audit_writer_password ;;
  export)
    run export --dsn "$reader" --password-file /run/secrets/pg_audit_reader_password --out /worm ;;
  verify)
    run verify --dsn "$reader" --password-file /run/secrets/pg_audit_reader_password "$@" ;;
  verify-bundles)
    run verify --bundles /worm "$@" ;;
  public-key)
    run public-key ;;
  *)
    echo "usage: keeper checkpoint|export|verify --public-key HEX|verify-bundles --public-key HEX|public-key" >&2
    exit 2 ;;
esac
