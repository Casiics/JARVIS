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

The task token must allow `agent.worker` (and `agent.supervisor` when a supervisor runs). Report the outcome to core with one `TaskMetadata` (state, duration, error class).

## Hermes Agent

`HermesRuntime(gateway, domain, command=("hermes",))` writes a throw-away `~/.hermes/config.yaml` per turn (`provider: custom`, the loopback endpoint as `base_url`, a random key, `platform_toolsets.cli: []`) and runs `hermes -z <prompt>` with only `HOME`, `PATH`, `LANG` set. Tool definitions Hermes sends are dropped before the gateway. In production prefix `command` with the sandbox runner. Hermes asks for a 64k context; with the 8k local models from decision 4 keep `GatewayRuntime` as the default.
