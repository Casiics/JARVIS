# JARVIS: Multi-Agent Platform (Hermes Core + n8n Execution Bus)

Read [`docs/architecture.md`](docs/architecture.md) before changing structure, data flows or security boundaries. Accepted decisions are in [`docs/decisions.md`](docs/decisions.md).

## 1. Engineering standards

- **Language:** all code comments, docstrings, commit messages and architecture docs are in English, regardless of the conversation language. Keep meaningful developer comments; remove resolved TODO markers.
- **Challenge anti-patterns:** if a request is insecure, tightly coupled or breaks domain isolation, say so and implement the clean alternative.
- **Python 3.12+ (uv):** `mypy --strict`; Pydantic v2 for every payload, tool input/output and state transition.
- **TypeScript 5.x (pnpm + Turborepo):** `strict: true`, `noUncheckedIndexedAccess: true`, no `any`; Zod at every runtime boundary, including WebSocket telemetry.

## 2. Non-negotiable security rules

1. **Domains are isolated.** The domains `core`, `trading`, `content` and `private` (Life-OS) never import from, call, or read the stores of another domain. Cross-domain data moves only as typed boundary events (`jarvis_core.contracts.events`), checked by `jarvis_core.policy.flows`.
2. **Classification is enforced in code.** Every payload carries its origin domain and a `DataClassification` (`PUBLIC`, `INTERNAL`, `CONFIDENTIAL`, `PRIVATE`, `SECRET`). Missing or unknown labels are treated as `PRIVATE`. Only deterministic code may change a label.
3. **PRIVATE stays local.** Anything `PRIVATE` or from the `private` domain goes only to local models via `jarvis_core.router`. There is no cloud route for the private domain; failing closed is required.
4. **No secrets in agent context.** Agents never receive API keys, OAuth tokens or broker keys. Secrets are read only by deterministic executors through `jarvis_core.secrets.SecretProvider` (SOPS + age, one key per domain). External calls go through n8n MCP triggers, the risk gate or the git gateway.
5. **LLMs propose, deterministic code decides.** External effects are `ActionRequest`s executed only by the action gate. Trading orders always pass the non-LLM risk engine.
6. **Audit everything, store no content in the audit log.** Every state transition, tool call, loop iteration and self-modification emits an `AuditLogEntry` (hashes and references only).
7. **Untrusted execution is sandboxed.** Code execution, scraping, browser automation and `yt-dlp` run only in the sandbox runner.
8. **Protected paths** (`core/jarvis_core/policy/`, `core/jarvis_core/gate/`, `core/jarvis_core/router/policy.py`, `core/jarvis_core/audit/`, `modules/mod-trading/risk/`, `policies/`, `docker/`, `secrets/`, `.github/`, `.claude/`, `CLAUDE.md`) are changed only with explicit human instruction. Self-improvement agents must never modify them.

## 3. Runtime hierarchy

1. **Tier 0a, n8n** (one instance per domain): inbound webhooks and polling, normalisation and labelling, OAuth lifecycle, outbound actions as MCP triggers. No LLM loops in n8n.
2. **Tier 0b, deterministic gate**: schema, lint, type and test checks on every worker output before Tier 1 is called. Failures go straight back to the worker.
3. **Tier 2, worker swarm**: one Hermes instance per domain, max 4 iterations per sub-task, Redis-backed FinOps circuit breaker (2.50 EUR/h, 15 EUR/day, plus per-domain budgets; private has none).
4. **Tier 1, supervisor**: one per domain; DAG planning, acceptance schemas, final semantic QA after Tier 0b. The private domain's supervisor is local. Core's task router issues signed task tokens and has no data access.

## 4. Self-improvement

- Verified multi-step solutions may be proposed as skills in `skills/<domain>/`; they land only via PR.
- Synthesised n8n workflows are validated in Tier 0b, deployed inactive, dry-run, then exported to `core/n8n-workflows/` and committed.
- Autonomous changes go to `auto-improve/<timestamp>-<topic>` branches through the git gateway, pass the full test suite in a sandbox and are merged by a human. Never push to `main`.

## 5. Commands

- Python: `uv sync` | `uv run pytest` | `uv run ruff check .` | `uv run ruff format --check .` | `uv run mypy` | `uv run mypy tests`
- Database tests: set `JARVIS_TEST_PG_DSN` to a disposable Postgres superuser DSN, then `uv run pytest -m db`
- Secrets: `scripts/secrets-bootstrap.sh` (once) | `scripts/secrets-unpack.sh` (before `docker compose up`)
- Frontend: `pnpm install` | `pnpm turbo run lint typecheck test build`
- Infrastructure: `docker compose -f docker/docker-compose.yml up -d`
