# Runbook: audit signatures, WORM export, workload certificates (Phase 7)

Phase 7 makes the audit chain tamper-evident beyond the database, gives each service a short-lived workload certificate, and lets agents propose skills only as pull requests. Design: [`architecture.md`](../architecture.md) sections 6.5, 7.1 and 8, decision 12 in [`decisions.md`](../decisions.md).

## 1. Audit checkpoints

A checkpoint is an Ed25519 signature over the chain head:

```
jarvis-audit-checkpoint/v1
<number of entries>
<entry_hash of the last entry>
<signed_at, UTC ISO 8601>
<key id: first 16 hex chars of SHA-256(raw public key)>
```

`audit.checkpoint_append()` in `pg-audit` accepts a checkpoint only if it names a real position whose `entry_hash` matches and does not go backwards. The table is append-only like the chain itself. Code: `core/jarvis_core/audit/checkpoint.py`, SQL: `docker/postgres/audit/sql/checkpoints.sql`.

What it catches: someone with database superuser rights who rewrites the chain from some point onward and recomputes every hash. The rewritten head no longer matches the last signature, and they do not hold the signing key (`core/audit_signing_key`, read only by `audit-keeper`).

### First setup

```bash
sudo scripts/secrets-bootstrap.sh     # new installs: creates core/audit_signing_key
docker compose -f docker/docker-compose.yml build audit-keeper
docker compose -f docker/docker-compose.yml run --rm audit-keeper public-key
```

Store the printed public key offline (password manager and on paper). Verification uses only this key, never the secret file.

Existing installs created before Phase 7: add `audit_signing_key`, `pki_ca_password` and `pki_provisioner_password` to `secrets/core.sops.yaml` (`sops secrets/core.sops.yaml`, values from `openssl rand -hex 32`), and apply the new SQL once, because the init scripts only run on an empty volume:

```bash
docker compose -f docker/docker-compose.yml exec -T pg-audit \
  psql -v ON_ERROR_STOP=1 -U postgres -d jarvis < docker/postgres/audit/sql/checkpoints.sql
```

### Host timers

| Job | Schedule | Command |
|---|---|---|
| Checkpoint | hourly | `docker compose -f docker/docker-compose.yml run --rm audit-keeper checkpoint` |
| WORM export | daily | `docker compose -f docker/docker-compose.yml run --rm audit-keeper export` |
| Workload certificates | every 8 hours | `docker compose -f docker/docker-compose.yml run --rm pki-issue` |

A checkpoint is written every hour even without new entries, so a missing hour is itself visible. Use systemd timers with `Persistent=true`, and alert (Telegram) when a run fails.

### Verifying

```bash
docker compose -f docker/docker-compose.yml run --rm audit-keeper verify --public-key <hex>
docker compose -f docker/docker-compose.yml run --rm audit-keeper verify-bundles --public-key <hex>
```

Both print the problems they find and exit non-zero. On a file chain (domain containers without Postgres): `python -m jarvis_core.audit verify --file <chain.jsonl> --public-key <hex>`; its checkpoints live next to it in `<stem>.checkpoints.jsonl`.

## 2. WORM export

`export` writes the entries since the last bundle up to the newest checkpoint as `audit-<from>-<to>.jsonl` plus `.manifest.json` into the `audit_worm` volume. Files are created exclusively and set to mode 0444; each bundle ends at a signed checkpoint, so it can be checked offline with the public key alone.

The volume is only the first copy. For real write-once storage, sync `audit_worm` daily to an offsite target with object lock (for example an S3 bucket with Object Lock in compliance mode and the retention from section 8 of the architecture), using credentials that can write but not delete.

## 3. Workload certificates (step-ca)

```bash
docker compose -f docker/docker-compose.yml run --rm pki-init   # once, no network
docker compose -f docker/docker-compose.yml up -d pki-ca
docker compose -f docker/docker-compose.yml run --rm pki-issue  # then every 8 hours
```

`pki-init` prints the root fingerprint; store it with the audit public key. `pki-issue` writes a 24-hour certificate with the SPIFFE ID `spiffe://jarvis.home/<domain>/<service>` for each line of `docker/pki/identities` into that domain's own volume (`certs_<domain>`), together with the root certificate. Only `pki-issue` joins `net-pki`; no domain network reaches the CA.

`pki-ca` keeps one capability, `NET_BIND_SERVICE`: the step-ca binary carries it as a file capability and the kernel refuses to start it otherwise.

## 4. Skill proposals

`jarvis_core.selfimprove.propose_skill` turns a verified multi-step solution into one new file `skills/<domain>/<name>/SKILL.md` and hands it to the git gateway, which opens an `auto-improve/*` PR. It refuses the private domain, skills without evidence (task ids), text that looks like a secret, and text that names a protected path. Skills from tainted tasks are marked as such in the PR.

## 5. Open points and residual risks

- **Certificates are issued, not yet used.** No service terminates or checks mTLS yet. Wiring them in (Envoy gateways first) is a follow-up; issued keys are root-owned 0600, so consumers will need matching ownership.
- **New files can call the broker.** The gateway and CODEOWNERS guard `executor.py` and `broker.py`, but a new file in `mod_trading/` could import the broker directly. Human review of every `auto-improve/*` PR covers this; a static import check is a candidate follow-up.
- **Signing key on the same host.** An attacker with root on the server could read the signing key and re-sign a rewritten chain. The offline public key, the hourly cadence and the offsite WORM copy limit this to entries not yet exported.
- **Domain chains.** Trading and private keep `audit_signing_key` secrets for their file chains; their keepers are wired when those domains run as services.
