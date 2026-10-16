# JARVIS

Personal multi-agent platform built on Hermes Agent (Nous Research) as the core runtime, with a supervisor-worker loop, n8n as the deterministic I/O bus, and a real-time command dashboard.

Four security domains share one platform but never share data, credentials or networks: **core**, **trading**, **content** and **private** (Life-OS).

- Target architecture and roadmap: [`docs/architecture.md`](docs/architecture.md)
- Architecture decisions: [`docs/decisions.md`](docs/decisions.md)
- Rules for coding agents: [`CLAUDE.md`](CLAUDE.md)

## Status

Phase 1 (foundation): core contracts, boundary policy, action gate, model routing policy, task state machine, hash-chained audit log, per-domain secrets and data stores. No agents run yet.

## Quick start

```bash
uv sync                      # Python 3.12+, managed with uv
uv run pytest                # unit and config tests
JARVIS_TEST_PG_DSN=postgresql://postgres:postgres@localhost:5432/postgres uv run pytest -m db

sudo JARVIS_AGE_DIR=/etc/jarvis/age scripts/secrets-bootstrap.sh   # once: keys + encrypted secrets
sudo scripts/secrets-unpack.sh                                      # decrypt to /run/jarvis/secrets
docker compose -f docker/docker-compose.yml up -d                   # per-domain data stores + Ollama
```

Models are pulled once with temporary internet access, see `docker/docker-compose.models.yml`.
