---
description: Scaffolds a new isolated JARVIS domain with Tier-0b/Tier-2/Tier-1 loop integration, its own memory namespace and dashboard telemetry.
---

Scaffold a new domain module named `$ARGUMENTS` in `modules/mod-$ARGUMENTS/`, following `docs/architecture.md` section 3.2.

1. **Files**
   - `modules/mod-$ARGUMENTS/__init__.py`
   - `schemas.py`: Pydantic v2 input/output models; every boundary model derives from `jarvis_core.contracts.events.BoundaryEvent`.
   - `deterministic_gate.py`: Tier-0b validator for schemas and business rules, runs before any supervisor call.
   - `worker_loop.py`: Tier-2 worker using `core/model-router` only, max 4 iterations, budget-aware.
   - `memory.py`: client for the domain's own stores (own Postgres instance, Qdrant where needed, cache namespace via `jarvis_core.router.cache`). No direct database access.
   - `actions.py`: builds `ActionRequest`s for external effects; never calls external APIs directly.
   - `telemetry.py`: metadata-only telemetry events (no prompts, outputs or raw data) and `AuditLogEntry` emission.
   - `skills/$ARGUMENTS/README.md`
   - `tests/modules/test_mod_$ARGUMENTS.py`: Tier-0b rejection, worker retry limit, supervisor approval, and a test that the domain cannot read another domain's store.
2. **Registration** (protected paths: ask the user before editing)
   - Add the domain to `jarvis_core.contracts.classification.Domain` and its routing rules to `core/jarvis_core/router/policy.py`.
   - Add its allowed boundary events to `core/jarvis_core/policy/flows.py`.
   - Add its database, network and secrets to `docker/docker-compose.yml` and `scripts/secrets-bootstrap.sh`.
3. **Checks**: run `uv run ruff check` and `uv run mypy --strict` on the new files. All comments in English.
