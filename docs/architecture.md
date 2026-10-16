# JARVIS Target Architecture

Status: accepted (2026-10-16). Decisions are recorded in [`decisions.md`](decisions.md).

## 1. Summary

JARVIS is a small platform, not a single agent. A central **control plane** (Hermes supervisor, task orchestrator, model router, policy engine, event bus, audit service, secret store) coordinates isolated **domain cells**: Trading, Content, Life-OS and Platform (reel ingestion and self-improvement). Each cell has its own Hermes worker pool, its own memory, its own secrets and its own network segment. Cells never talk to each other directly; they exchange typed, classified events over a bus controlled by the policy engine.

### Guiding principles

1. **Isolation through infrastructure, not prompts.** What an agent cannot reach, it cannot leak. Every boundary is a network segment, a database role or a missing credential.
2. **Data only flows into stricter zones.** Data may move into a zone of equal or higher classification (public to private), never the other way without an explicit, deterministic declassification step.
3. **LLMs propose, deterministic code decides.** Every action with an external effect (order, post, email, calendar entry, merge) passes a non-LLM gate and, depending on risk, human approval.
4. **No agent ever sees a secret.** Credentials live in the secret store and in execution services (n8n, trading executor, git gateway), never in a context window.
5. **Everything is traceable.** Every decision, loop iteration and action produces an immutable audit entry, without private content ending up in the audit log.
6. **Self-improvement cannot touch its own guardrails.** Gates, router policy, audit, policy engine and the risk engine are protected paths that only a human changes.

## 2. System overview

```text
                 External world: Telegram · Instagram · CalDAV/IMAP · Broker API · Model APIs
                        │                         │                          │
                        ▼                         ▼                          ▼
              ┌──────────────────┐   ┌──────────────────────┐   ┌──────────────────┐
              │ n8n-public (0a)  │   │ API gateway + UI     │   │ n8n-life (0a)    │──┐ direct, no bus
              └────────┬─────────┘   └──────────┬───────────┘   └──────────────────┘  │
                       ▼                        ▼                                     │
┌──────────────────────────────────────────────────────────────────────────────┐      │
│ CONTROL PLANE (never sees PRIVATE content)                                   │      │
│ Hermes supervisor (Tier 1) · Orchestrator · Model router · Policy engine     │      │
│ Event bus (Redis Streams) · Audit service · Secret store                      │      │
└───────┬──────────────────┬──────────────────┬──────────────────┬─────────────┘      │
        ▼                  ▼                  ▼                  ▼                    │
┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐             │
│ Trading cell │   │ Content cell │   │ Platform cell│   │ Life-OS cell │◄────────────┘
│ CONFIDENTIAL │   │ INTERNAL     │   │ PUBLIC/INT.  │   │ PRIVATE      │
└──────┬───────┘   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
       ▼                  └──────────┬───────┴──────────────────┘
┌──────────────────┐   ┌─────────────▼──────────────────────────────────────────┐
│ Trading executor │   │ Shared services: sandbox runner · Ollama + Whisper     │
│ (broker keys)    │   │ (router only, no egress) · git gateway                 │
└──────────────────┘   └────────────────────────────────────────────────────────┘
```

Events enter only through n8n or the API gateway. The control plane plans and controls, cells do the work, and only execution services hold credentials. Life-OS receives its data directly from its own n8n instance and reports only metadata to the control plane.

## 3. Components

### 3.1 Control plane

| Component | Responsibility | Technology | Must never |
|---|---|---|---|
| Hermes supervisor (Tier 1) | Decomposes goals into a task DAG, defines acceptance schemas, performs final semantic QA after Tier 0b | Hermes Agent, high-reasoning model via the router | Call tools with external effects; see Life-OS content |
| Task orchestrator | Owns each task's state machine (section 4), retries, timeouts, escalation | Python service, state in Postgres, queue in Redis Streams | Call models itself |
| Model router | Picks the model per call by cell, classification and budget; PII scanner; FinOps circuit breaker | LiteLLM proxy plus a JARVIS policy layer | Forward a PRIVATE request to a cloud endpoint (fail closed) |
| Policy engine | Decides every boundary crossing: which cell may read which event, call which tool, move which classification where | Open Policy Agent (Rego), policies as code in this repo | Be modified by agents |
| Event bus | Typed events between cells and the control plane | Redis Streams with one ACL user per cell (NATS once a second host is added) | Carry raw PRIVATE content |
| Audit service | Accepts `AuditLogEntry` records, hash-chains them, writes append-only | Python service, Postgres schema `audit` | UPDATE or DELETE |
| Secret store | Single source of credentials, rotation, issues secrets only to execution services | OpenBao/Vault or Infisical, self-hosted | Hand a secret to an agent container |
| API gateway | Single entry point for the dashboard and mobile clients, auth, WebSocket telemetry | FastAPI, Pydantic/Zod at the boundary | Answer without device and user identity |

### 3.2 Domain cells

Every cell is built from the same five parts, so new domains can be scaffolded with `/scaffold-module`:

1. **Hermes worker pool** (Tier 2) with the cell's skills (`skills/<cell>/`), max 4 iterations per sub-task. One Hermes instance per cell with its own data directory.
2. **Deterministic gate** (Tier 0b) with cell-specific rules: Pydantic schemas, linters, tests, business rules.
3. **Memory service** with its own Postgres schema, Qdrant collection and cache namespace. Agents query the service, never the database.
4. **Tool allowlist**: the n8n workflows and MCP servers this cell may call, nothing else.
5. **Action gate**: the last gate before any external effect, deterministic and with human approval where required.

| Cell | Worker models | Supervisor | External execution | Specifics |
|---|---|---|---|---|
| Trading | Local (analysis, backtests) | Cloud Tier 1, pseudonymised data only | Trading executor (deterministic service with broker keys), not n8n | Non-LLM risk engine: position size, daily loss, drawdown, instrument list, kill switch; paper trading first |
| Content | Cloud Tier 2 (research, text), local for media | Cloud Tier 1 | `n8n-public` (social APIs) | Publishing always needs human approval; AI-generated content is labelled |
| Life-OS | Local only (Ollama) | Local supervisor inside the cell | `n8n-life` (CalDAV, IMAP) | No internet egress except through `n8n-life`; no cloud route is configured at all |
| Platform | Local | Cloud Tier 1 for reviews | Git gateway (`auto-improve/*` only) | Works only in sandboxes; protected paths are immutable |

### 3.3 Shared execution services

- **n8n instances** (Tier 0a): all inbound webhooks and polling, normalisation, OAuth lifecycle, outbound actions exposed as MCP triggers. Two instances: `n8n-life` and `n8n-public`. No LLM loops inside n8n.
- **Sandbox runner**: ephemeral rootless containers (gVisor or Firecracker), read-only filesystem, no network or a per-run egress allowlist; for code execution, `yt-dlp`, browser automation and backtests.
- **Ollama and Faster-Whisper**: one shared, stateless inference service without egress, reachable only through the router so budget, audit and classification apply. A dedicated Life-OS instance follows once a second GPU exists.
- **Trading executor**: places orders with its own broker keys, only for signals that passed the risk engine.
- **Git gateway**: the only component allowed to push for agents; fine-grained token, `auto-improve/*` branches only, rejects changes to protected paths.

## 4. Supervisor-worker loop

Each task is a state machine in the orchestrator, not a free-form agent conversation.

```text
Supervisor plans (DAG + acceptance schema)
   → Worker runs (Tier 2)
   → Tier 0b passes?      no → error trace back to worker (no Tier 1 call)
   → Supervisor QA ok?    no → feedback back to worker
   → External effect?     no → DONE
                          yes → Action gate (rules + human if required)
                                  approved → execute via n8n / trading executor → DONE
                                  rejected → ESCALATED
Worker reaches 4 iterations or budget is exhausted → ESCALATED (human decides in the dashboard)
```

Rules:

- **Acceptance schema up front.** The supervisor fixes the Pydantic schema and tests a result must pass; Tier 0b checks exactly that.
- **Workers have no action tools.** They read, compute, write code or text. An intended external effect becomes an `ActionRequest` that only the action gate executes.
- **Small context.** Each sub-task receives only the data of its cell and step.
- **Budget per task and per cell.** The router counts cost in Redis; the circuit breaker applies globally (2.50 EUR/h, 15 EUR/day) and per cell.
- **Every transition is audited**, with model, cost and gate result.

## 5. Domains and boundaries

### 5.1 Data classification

Every data object and event carries two labels: the **origin cell** and a **class**. Labels are set at the source (n8n normalisation or memory service) and can only be changed by deterministic code, never by an LLM.

| Class | Examples | Allowed models | Storage | Dashboard |
|---|---|---|---|---|
| `PUBLIC` | Reels, market data, published posts, research | Local and cloud | Any cell | Content visible |
| `INTERNAL` | Content drafts, strategy descriptions, skills, code | Local and cloud (no training opt-in) | Own cell | Content visible |
| `CONFIDENTIAL` | Positions, balances, order history, platform accounts | Local; cloud only after pseudonymisation by the gate | Own cell, encrypted | Aggregated |
| `PRIVATE` | Calendar, email, health, journal, contacts | Local only | Life-OS only, encrypted per record | Metadata only; content after re-authentication |
| `SECRET` | API keys, OAuth tokens, broker keys | No model | Secret store only | Never |

### 5.2 Boundary rules

1. **No direct connection between cells.** Networks, database roles and bus ACLs allow only cell ↔ control plane.
2. **Upgrading is allowed, downgrading is not.** A public reel may land in Life-OS. Life-OS content never leaves Life-OS. Trading data leaves its cell only as pseudonymised, aggregated metrics.
3. **Crossings are schema-typed events.** No free text or prompts cross cells, only Pydantic models such as `TradingIdeaProposed` or `ContentInspirationStored`, each checked by the policy engine.
4. **The supervisor sees what it needs.** For Life-OS the central supervisor sees only task metadata; content QA happens in the cell's local supervisor.
5. **Fail closed.** A missing or unknown label is treated as `PRIVATE` and processed locally.
6. **External content is data, not instructions.** Anything from reels, web pages, emails or posts is marked untrusted. An agent that has read such content cannot trigger an external effect in the same step; a separate step with clean context and the action gate decides.

### 5.3 Allowed flows between cells

| From → to | Allowed | Form |
|---|---|---|
| Platform (reels) → Trading | Yes | `TradingIdeaProposed` into the backtest queue, never into execution |
| Platform (reels) → Content | Yes | `ContentInspirationStored` (hook, structure, source) |
| Platform (reels) → Life-OS | Yes | `LifeTipStored`; inbound only |
| Trading → Content | After approval | Publishable metrics, approved by hand |
| Life-OS → any other cell | No | Only via an explicit, logged approval of a single deterministically produced summary |
| Any cell → Platform | Yes | Telemetry and audit metadata for the self-audit only |

## 6. Key data flows

### 6.1 Inbound event to action (base pattern)

1. An external event hits the responsible n8n instance.
2. n8n verifies signature and sender, normalises to schema-conformant JSON and sets cell and class.
3. The event goes onto the bus; the policy engine checks the target cell may receive it.
4. The orchestrator creates a task; the supervisor (central or local, depending on class) plans the DAG.
5. Workers execute, Tier 0b checks every result, the supervisor checks semantics.
6. An intended external effect becomes an `ActionRequest`; the action gate checks it deterministically and requests human approval through the dashboard where required.
7. The execution service (n8n workflow or trading executor) acts with its own credentials and reports back.
8. Every step writes an audit entry; metadata-only telemetry goes to the dashboard.

### 6.2 Reel to knowledge and action

1. A reel URL arrives via Telegram or the folder watcher in `n8n-public`.
2. The sandbox runner downloads it with `yt-dlp`, egress limited to the Instagram CDN.
3. Locally, Faster-Whisper transcribes and the vision model reads keyframes (0 EUR).
4. A local worker builds a `ReelKnowledgeArtifact`; Tier 0b checks the schema and the rule "utility > 0.5 requires actionable steps".
5. The artifact stays `PUBLIC` but is marked **untrusted** because it contains third-party text.
6. Depending on `target_module` a boundary event is emitted. A skill candidate never goes straight into `skills/`; it follows the self-improvement flow.

### 6.3 Trading idea to order

1. An idea arrives as `TradingIdeaProposed` in the trading cell.
2. Workers express a strategy as code; Tier 0b checks types, tests and forbidden constructs (no network, no file access).
3. The backtest runs in the sandbox runner with historical data and no broker access.
4. The supervisor reviews results and assumptions. Passing means **paper trading**, not real money.
5. Going live is a human decision per strategy with a fixed capital limit.
6. Live signals pass the **risk engine** (plain code): position size, daily loss, drawdown, instrument list. Only passing signals reach the **trading executor**.
7. The kill switch (dashboard, Telegram command, automatic thresholds) stops all orders immediately and is independent of the LLM path.

No LLM ever places an order or knows broker credentials.

### 6.4 Life-OS (fully local)

1. `n8n-life` fetches calendar (CalDAV) and email (IMAP) and writes directly to the Life-OS memory service, class `PRIVATE`.
2. Local supervisor and workers plan, summarise and remind. All model calls go to Ollama; the router has no cloud route configured for this cell.
3. Actions (create event, draft reply) pass the action gate; emails are drafts by default.
4. Only metadata goes to the control plane: task state, duration, error class.

### 6.5 Self-improvement

1. Nightly at 03:00 the self-audit reads audit metadata: tasks with more than 2 retries, escalations, cost outliers, Tier 0 failure patterns. No Life-OS content.
2. A refactoring worker works on a sandboxed clone and proposes prompt, skill or code changes.
3. The full test suite runs in the sandbox.
4. The git gateway pushes `auto-improve/<date>-<topic>` and opens a PR. Changes to protected paths are rejected (see section 7.5).
5. A human merges; the dashboard shows the diff in the approval deck.

## 7. Zero-trust security

### 7.1 Identity

- Every service has its own workload identity (internal CA via step-ca, later SPIFFE/SPIRE); mTLS between services. Sharing a Docker network grants no trust.
- Every agent run has its own identity: cell, task id, role. The policy engine authorises every tool call on that identity.
- Humans sign in to the dashboard with a passkey, reachable only over Tailscale. Approvals for real-money trading, publishing and merges require step-up confirmation.

### 7.2 Secrets

- The secret store is the only source; `.env` files hold references only.
- Secrets are issued only to execution services: n8n instances (each with its own `N8N_ENCRYPTION_KEY`), trading executor, git gateway, router (model API keys).
- Agent containers receive no secrets. Workers authenticate to the router with their workload identity.
- Broker keys: trade-only, no withdrawal, IP-bound where the broker supports it.
- Automatic rotation; one command revokes all secrets of a cell.

### 7.3 Network

| Segment | May talk to | Internet egress |
|---|---|---|
| `net-control` | All cells (bus, router, audit only), gateway | Model APIs only, via egress proxy |
| `net-trading` | Control plane, trading executor | Market data sources via proxy |
| `net-trading-exec` | Trading cell (approved signals only), broker API | Broker only |
| `net-content` | Control plane, `n8n-public` | Research via allowlisted proxy |
| `net-life` | Control plane (metadata only), `n8n-life`, Ollama | None |
| `net-sandbox` | No inbound | Per run, default none |
| `net-data` | Memory services and audit service only | None |

All ports bind to `127.0.0.1` or internal Docker networks; external access only via Tailscale and Caddy (mTLS). An egress proxy with a per-segment domain allowlist logs every outbound call.

### 7.4 Sandboxing and supply chain

- Untrusted execution runs in ephemeral rootless containers with gVisor or Firecracker, read-only filesystem, CPU/RAM/time limits.
- Images pinned by digest, signed with Cosign, SBOM per build (CRA). Renovate proposes updates as PRs.
- Prompt injection is assumed: protection comes from missing permissions and the action gate, not from filters alone.

### 7.5 Protected paths

Only a human may change these; the git gateway rejects agent changes and CODEOWNERS requires owner review:

- `core/deterministic-gate/`, `modules/mod-trading/risk/`, `core/model-router/policy/`, `policies/`, `core/audit/`, `docker/`, `.github/`, `.claude/`, `CLAUDE.md`

### 7.6 Approval tiers for actions

| Tier | Examples | Approval |
|---|---|---|
| A: read-only, internal | Research, summary, backtest | Automatic |
| B: reversible, private | Calendar draft, email draft, create branch | Automatic, visible in dashboard |
| C: externally visible | Publish post, send email, paper order | Human, one click |
| D: money or code into `main` | Real-money order outside an approved strategy, switching a strategy live, merge | Human with step-up |

Real-money orders of an already approved strategy run without a click, but only within the risk engine's limits.

## 8. Persistence, memory and audit

| Store | Content | Isolation |
|---|---|---|
| Postgres `control` | Tasks, DAGs, states, approvals, budgets | Orchestrator and gateway only |
| Postgres `trading`, `content`, `platform` | Cell data | One role per cell, no access to other schemas |
| Postgres `life` (own instance) | Private data | Own instance in `net-life`, per-record encryption |
| Postgres `audit` | Audit chain | `audit_writer` INSERT only; `audit_reader` for self-audit and dashboard |
| Qdrant | One collection per cell; Life-OS in its own instance | API key per collection, access via memory service only |
| Redis | Bus (streams), queues, budget counters | ACL user per cell with key prefix; Life-OS uses its own Redis for internal queues |
| Skills | `skills/<cell>/*.md` in this repo | A cell loads only its own skills plus `skills/shared/` |

The semantic cache is kept per cell and excludes `PRIVATE` and `CONFIDENTIAL`.

**Hermes memory.** Each cell runs its own Hermes instance with its own data directory so memory and learned skills never mix across domains. Generated skills reach the repo only through the self-improvement flow.

**Audit.**

- Each entry: timestamp, cell, task, agent identity, model and version, event type, decision, cost, content hash, content reference, previous entry hash.
- **Content is never stored in the audit log.** It lives in the cell's store, encrypted with a per-record key. A GDPR erasure destroys the key; the chain stays valid, the content becomes unreadable.
- The chain head is anchored daily to a separate read-only backup so tampering becomes detectable.
- Retention: audit metadata at least 6 months; trading decisions per tax and record-keeping obligations.

**Backup.** Encrypted nightly backups per instance (e.g. restic) to a target outside the homelab; Life-OS backups with their own key; restores tested regularly.

## 9. Command dashboard

The dashboard observes and approves; it is not a privileged second control channel. It talks only to the API gateway.

- **Live DAG**: task states from the loop (React Flow), coloured by cell; n8n executions as nodes.
- **Approval deck**: open `ActionRequest`s of tiers C and D with rationale, diff or preview and risk check result.
- **FinOps**: cost per cell, model and task against budget; circuit breaker state.
- **Trading cockpit**: aggregated positions, risk engine limits, kill switch.
- **Audit explorer**: search with chain integrity check.
- **Life-OS view**: metadata only by default; content only after re-authentication and via a direct connection into the Life-OS cell.

**Telemetry contract.** A telemetry event carries task id, cell, node, state, duration, cost, model class and error class, never prompts, outputs or raw data. All WebSocket messages are validated with Zod.

Voice or Telegram control takes the same path as any other event and has no extra rights. Tier D approvals are not possible via Telegram; the kill switch is.

## 10. Deployment and operations

- **Workstation phase**: one `docker compose` stack, but already with the separate networks, roles and instances described here. Isolation is not retrofitted later because it shapes data models and interfaces.
- **Homelab phase**: identical stack on the server, access only via Tailscale and Caddy (mTLS), no open router ports. Life-OS preferably on the physically best protected machine with disk encryption.
- **Observability**: OpenTelemetry traces through orchestrator, router, workers and execution services; Prometheus metrics; Loki logs without content.
- **Alerts** via Telegram: circuit breaker tripped, risk engine rejections accumulating, audit chain inconsistent, egress proxy blocking unexpected targets.
- **Emergency stops**: global pause for all agents, trading kill switch, revoke all secrets of a cell.

## 11. Roadmap

Each phase ends with an exit criterion that must hold before the next starts.

1. **Foundation**: monorepo, compose with separate networks, secret store, Postgres schemas and roles, audit service with hash chain, event schemas, router with classification rules, orchestrator state machine.
   Exit: a test proves a `PRIVATE` request cannot reach a cloud endpoint and a cell cannot read another cell's data.
2. **First vertical slice: reel pipeline.** Touches sandbox, local models, Tier 0b, supervisor and boundary events, but no money and no private data.
   Exit: 20 real reels processed, cost and audit visible in the dashboard.
3. **Dashboard skeleton**: live DAG, FinOps, approval deck, audit explorer.
   Exit: a tier C action can only be approved via the deck.
4. **Content cell** with publishing only after approval.
5. **Life-OS cell** with its own n8n, Postgres and Qdrant instances and a local supervisor.
   Exit: a network test shows `net-life` cannot reach the internet.
6. **Trading cell** with backtests and paper trading only; risk engine and kill switch with dedicated tests.
   Exit: several weeks of paper trading without risk-limit violations before real money is technically possible.
7. **Self-improvement** with git gateway, protected paths and the nightly audit.
   Exit: a deliberate attempt to change the risk engine via an auto-PR is rejected by the gateway.
