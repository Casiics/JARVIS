# Runbook: orchestrator and supervisor-worker loop

Architecture: [`../architecture.md`](../architecture.md), section 4.

## What runs where

| Piece | Process | Talks to |
|---|---|---|
| Orchestrator (`jarvis_core.orchestrator.service`) | API gateway (`python -m jarvis_core.api`), background thread | Boundary mailboxes only; audit writer; telemetry hub |
| Supervisor-worker loop (`jarvis_core.orchestrator.loop`) | Inside each domain's worker | Its `AgentRuntime` |
| `GatewayRuntime` | Inside the domain | Inference gateway |
| `HermesRuntime` | Inside the domain, one `hermes -z` per turn (in the sandbox in production) | A loopback endpoint that forwards to the inference gateway |

## Start

The orchestrator starts with the API gateway; nothing extra to run:

```bash
uv run --extra gateway python -m jarvis_core.api --rp-id … --origin … \
  --boundary-dir ~/.local/share/jarvis-deck-local/boundary --tick-seconds 2
```

Tasks persist in `<data-dir>/tasks.json`. Every two seconds it reads the domains' reports and escalates tasks whose domain stayed silent for two hours (the deadline restarts with each report).

## Check

From the terminal (read-only, safe next to the running gateway; for `scripts/deck-local.sh` the data dir is `~/.local/share/jarvis-deck-local/data`):

```bash
uv run python -m jarvis_core.orchestrator --data-dir ~/.local/share/jarvis-deck-local/data status
```

In the deck, **Agenten** shows the swarm, every task's stage and the escalations; failed reels can be queued again from there in one click (`POST /api/content/reels/retry`).

- Deck: the live view shows each task's transitions (`/ws/telemetry`); `GET /api/tasks` lists the last 100 tasks as metadata (kind, domain, state, duration, error class). Goals themselves are kept as SHA-256 only.
- A task stuck in `planned`: the domain worker is not running or not reading the same `--boundary-dir`.
- A task escalated with `timeout`: the domain stopped reporting; check the worker's log.
- Reports from a domain the task was not sent to, or for unknown tasks, are dropped and audited as `deny`.

## Using the loop in a domain

```python
loop = SupervisorWorkerLoop(TaskMachine(audit, actor), telemetry=…)
outcome = loop.run(
    WorkOrder(claims=verified_claims, trace_id=…, instruction="…",
              label=DataClassification.PUBLIC, inputs=(("transcript", text),)),
    Acceptance(MyResult, checks=(my_rule,)),
    RuntimeWorker(GatewayRuntime(gateway, Domain.CONTENT)),
    RuntimeSupervisor(GatewayRuntime(gateway, Domain.CONTENT)),
)
```

The task token must allow `agent.worker` (and `agent.supervisor` when a supervisor runs). For live stages in the deck, pass `MetadataReporter(Mailbox(boundary_dir, Domain.CONTENT)).sink` as the loop's telemetry: each state change goes to core as one `TaskMetadata` (state, duration, error class), nothing else.

## Smoke test on the workstation

One full loop (worker, Tier 0b, supervisor) on the local models, nothing leaves the machine:

```bash
uv run python -m jarvis_core.agents smoke --ollama-url http://localhost:11434
```

It prints each transition and a metadata summary (state, attempts, tokens, audit chain). Needs the models from decision 4 (`ollama pull qwen2.5-coder:14b-instruct-q4_K_M deepseek-r1:8b`; the smoke task uses `deepseek-r1:8b`). `error_class: model_unavailable` means Ollama is not reachable at that URL.

## Hermes Agent

Hermes Agent asks for at least 64k context, more than the 8k the decision-4 models run at. Its turns therefore route as model class `agent`: **Hermes 3 8B** (`ollama/hermes3:8b`, Llama 3.1 with a native 128k window) loaded with a fixed 64k window. Estimated VRAM on the RTX 4070 Ti: about 4.7 GB of Q4 weights plus a 64k KV cache of about 8.6 GB in f16 or about 4.3 GB in q8_0, so it fits only with a quantised cache. Not yet measured on the card.

```bash
# Ollama: flash attention and a q8_0 KV cache (set where Ollama runs, then restart it)
OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 ollama serve
ollama pull hermes3:8b
# Hermes Agent CLI (see its quickstart), then:
uv run python -m jarvis_core.agents smoke --runtime hermes
```

`HermesRuntime(gateway, domain)` writes a throw-away `~/.hermes/config.yaml` per turn (`provider: custom`, a task-scoped loopback endpoint as `base_url`, a random key, `platform_toolsets.cli: []`) and runs `hermes -z <prompt>` with only `HOME`, `PATH`, `LANG` set. Every request Hermes makes goes through the inference gateway (routing policy, budget, audit); tool definitions it sends are dropped. In production prefix `command` with the sandbox runner. The gateway now loads every local model with its own fixed window (`router/catalog.py: local_context`), so Ollama does not reload models between calls; this also fixes the 32k coding fallback, which used to run at 8k.

## Audit anchors from the domains

Domain processes keep their own hash-chained audit file because they cannot reach pg-audit. After every reel the content worker sends an `AuditAnchor` (chain name, entry count, head hash; nothing else) to core. The orchestrator writes each new anchor into core's central audit log as `audit_anchor:<domain>/<chain>` with the head in `content_hash` and `entries=<n>` in `content_ref`, and keeps the latest one per chain in `<data-dir>/audit-anchors.json`.

An anchor with fewer entries than the last one, or the same count with a different head, means the local file was truncated or rewritten. It is logged as a `deny` with `policy_id = audit-anchor-rollback`, and the last good head stays the reference. To check a file by hand against any anchor from the central log, use `jarvis_core.boundary.anchor.matches_anchor(entries, count, head_hash)`.

The trading executor and the private assistant can send anchors the same way (`jarvis_core.boundary.anchor.anchor_event`) once they run with a boundary mailbox.
