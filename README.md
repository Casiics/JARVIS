# JARVIS

Personal multi-agent platform built on Hermes Agent (Nous Research) as the core runtime, with a supervisor-worker loop, n8n as the deterministic I/O bus, and a real-time command dashboard.

Four security domains share one platform but never share data, credentials or networks: **core**, **trading**, **content** and **private** (Life-OS).

- Target architecture and roadmap: [`docs/architecture.md`](docs/architecture.md)
- Architecture decisions: [`docs/decisions.md`](docs/decisions.md)
- Rules for coding agents: [`CLAUDE.md`](CLAUDE.md)

## Status

- Phase 1 (foundation), done: core contracts, boundary policy, action gate, model routing policy, task state machine, hash-chained audit log, per-domain secrets and data stores.
- Phase 2 (reel pipeline), implemented: sandboxed download and media extraction, local transcription and keyframe reading, a quarantined extraction worker without tools, Tier 0b gate, follow-up proposals through the boundary policy, cost and audit report. See [`docs/runbooks/reel-pipeline.md`](docs/runbooks/reel-pipeline.md). The exit criterion (20 real reels) needs a run on the workstation.
- Phase 3 (command deck), implemented: API gateway with passkey login, approval deck in which every decision is its own passkey ceremony, single-use approval grants checked before execution, live metadata-only telemetry, and the seven-dashboard frame in the Glass HUD style. See [`docs/runbooks/command-deck.md`](docs/runbooks/command-deck.md).
- Phase 4 (isolation services), implemented: egress proxies with per-domain allowlists, one n8n per domain, a policy gateway (Envoy + OPA) as the only path from agents to their tools, a CalDAV/IMAP-only exit for the private domain, and the git gateway for `auto-improve/*` PRs. Live network tests run in Docker. See [`docs/runbooks/isolation-services.md`](docs/runbooks/isolation-services.md).
- Phase 5 (private domain), implemented: calendar and mail sync, mail triage, a daily briefing and reply drafts, all on local models with Tier 0b checks and a local supervisor. The assistant container reaches only your CalDAV and IMAP hosts and Ollama; drafts are never sent. See [`docs/runbooks/private-domain.md`](docs/runbooks/private-domain.md).

## Quick start

```bash
uv sync                      # Python 3.12+, managed with uv
uv run pytest                # unit and config tests
JARVIS_TEST_PG_DSN=postgresql://postgres:postgres@localhost:5432/postgres uv run pytest -m db

sudo JARVIS_AGE_DIR=/etc/jarvis/age scripts/secrets-bootstrap.sh   # once: keys + encrypted secrets
sudo scripts/secrets-unpack.sh                                      # decrypt to /run/jarvis/secrets
docker compose -f docker/docker-compose.yml up -d                   # per-domain data stores + Ollama
```

Deck: `pnpm install && pnpm turbo run lint typecheck test build`, then `uv run python -m jarvis_core.api --help`.

Models are pulled once with temporary internet access, see `docker/docker-compose.models.yml`.
