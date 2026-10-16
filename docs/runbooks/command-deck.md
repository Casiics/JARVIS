# Runbook: command deck and API gateway (Phase 3)

The deck observes and approves. It talks only to the API gateway (`core/jarvis_core/api/`), and the gateway is the only place a held action can be decided. Design: [`architecture.md`](../architecture.md) sections 3.1, 7.1, 7.6 and 9.

## How an approval works

1. The action gate holds an `ActionRequest` (tier C or D, or any tainted write) and `jarvis_core.approvals.store.hold()` queues it.
2. The deck lists it under "Wartet auf dich". The private domain shows metadata only.
3. Approve or reject starts a WebAuthn ceremony bound to this approval, this decision and the request's hash, with user verification required. Login assertions, other approvals' ceremonies, other origins and replays are refused.
4. The gateway signs an `ApprovalGrant` (HMAC, `approval_grant_key`, 10 minutes, single use) that names the exact request by hash, and audits the decision.
5. Executors call `jarvis_core.approvals.grant.authorize_execution()`. Without a valid grant for exactly that request, a held action does not run.

This is the Phase 3 exit criterion; `tests/api/test_deck_approvals.py` proves it.

## Run it on the workstation

```bash
# Secrets: approval_grant_key and deck_bootstrap_code are created by scripts/secrets-bootstrap.sh
# (existing installations: add both keys to secrets/core.sops.yaml with `sops`).
sudo scripts/secrets-unpack.sh

pnpm install && pnpm turbo run build           # builds apps/deck/dist
uv run python -m jarvis_core.api \
    --rp-id jarvis.<tailnet>.ts.net --origin https://jarvis.<tailnet>.ts.net \
    --static apps/deck/dist --audit-dsn "$JARVIS_AUDIT_WRITER_DSN"
```

### TLS on the tailnet (`edge-caddy`)

The compose service `edge-caddy` (profile `edge`) terminates TLS 1.3 on the host's tailnet address and proxies to the gateway on `127.0.0.1:8080`, including the WebSocket at `/ws/telemetry`. It binds nothing but the tailnet address, has no admin API and no ACME client, and refuses to start without `JARVIS_TAILNET_IP` (a `100.x` address) and `JARVIS_DECK_HOST`. The certificate comes from `tailscale cert` on the host, so Caddy never touches tailscaled:

```bash
sudo install -d -m 0750 -o root -g 1000 /etc/jarvis/tls
sudo tailscale cert --cert-file /etc/jarvis/tls/deck.crt --key-file /etc/jarvis/tls/deck.key \
    jarvis.<tailnet>.ts.net
sudo chgrp 1000 /etc/jarvis/tls/deck.* && sudo chmod 0640 /etc/jarvis/tls/deck.*
export JARVIS_DECK_HOST=jarvis.<tailnet>.ts.net JARVIS_TAILNET_IP=$(tailscale ip -4)
docker compose -f docker/docker-compose.yml --profile edge up -d edge-caddy
```

The deck is then at `https://jarvis.<tailnet>.ts.net:8443` (`JARVIS_DECK_TLS_PORT` changes the port); start the gateway with that exact `--origin` and `--rp-id jarvis.<tailnet>.ts.net`. Renew the certificate with a monthly host timer running the `tailscale cert` line, then `docker compose ... restart edge-caddy`. WebAuthn needs a real https origin (or `http://localhost` for local trials).

**First start:** open the deck, choose "Erster Start: Passkey einrichten" and enter the bootstrap code from `/run/jarvis/secrets/core/deck_bootstrap_code`. The code works only while no passkey exists. Further devices are added from a signed-in session.

**Local trial without a tailnet:** `scripts/deck-local.sh` builds the deck, creates throwaway secrets and starts it on `http://localhost:8080`, printing the setup code. It passes `--rp-id localhost --origin http://localhost:8080`; cookies are then sent without `Secure`, and the audit log is the file chain `<state>/data/audit.jsonl`.

## Security properties

- Session cookie: random, stored server-side as a hash, `HttpOnly`, `Secure`, `SameSite=Strict`, 8 hours.
- Every POST must come from the deck's origin as JSON; everything else gets 403 or 415.
- CSP `default-src 'self'`, `frame-ancestors 'none'`, no referrer, no caching.
- Telemetry over WebSocket needs a session and the deck origin. Events are metadata only (`contracts/telemetry.py`, mirrored by Zod in `apps/deck/src/schemas.ts`); anything with extra fields is dropped on both sides.
- Passkeys and approvals are stored as 0600 JSON files under `--data-dir` until pg-core takes them over together with the orchestrator service. Without `--audit-dsn` the gateway keeps a hash-chained file log (`--audit-file`, default `<data-dir>/audit.jsonl`) that is verified on every start; a broken chain stops the gateway. Memory-only audit needs `--audit-memory` and is refused for an https origin.

## Known limits

- No domain feeds the approval queue across processes yet. The orchestrator and domain services arrive with Phases 5 and 6. Until then, approvals are queued in-process with `hold()`.
- Five of the seven dashboards are a frame with navigation and deep links. Content has the reel intake (docs/runbooks/reel-pipeline.md); the gateway's `--boundary-dir` (default `/var/lib/jarvis/boundary`) must match the reel worker's.
- Kill switch and daily spend in the top bar arrive with the trading domain and the FinOps service (Phase 6), together with the services they act on.
