---
description: Scaffolds a new isolated JARVIS domain cell with Tier-0b/Tier-2/Tier-1 loop integration, its own memory namespace and dashboard telemetry.
---

Scaffold a new domain cell named `$ARGUMENTS` in `modules/mod-$ARGUMENTS/`, following `docs/architecture.md` section 3.2.

1. **Files**
   - `modules/mod-$ARGUMENTS/__init__.py`
   - `schemas.py`: Pydantic v2 input/output models; every model carries `origin_cell` and `DataClassification`.
   - `deterministic_gate.py`: Tier-0b validator for schemas and business rules, runs before any supervisor call.
   - `worker_loop.py`: Tier-2 worker using `core/model-router` only, max 4 iterations, budget-aware.
   - `memory.py`: client for the cell's own memory service (own Postgres schema, Qdrant collection, cache namespace). No direct database access.
   - `actions.py`: builds `ActionRequest`s for external effects; never calls external APIs directly.
   - `telemetry.py`: metadata-only telemetry events (no prompts, outputs or raw data) and `AuditLogEntry` emission.
   - `skills/$ARGUMENTS/README.md`
   - `tests/modules/test_mod_$ARGUMENTS.py`: Tier-0b rejection, worker retry limit, supervisor approval, and a test that the cell cannot read another cell's store.
2. **Registration** (protected paths: ask the user before editing)
   - Add the cell's routing and classification rules to `core/model-router/policy/`.
   - Add the cell's allowed events and tools to `policies/`.
3. **Checks**: run `uv run ruff check` and `uv run mypy --strict` on the new files. All comments in English.
