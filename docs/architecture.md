# JARVIS Target Architecture

Status: accepted (2026-10-16), aligned the same day with the Zero-Trust security concept. Decisions are recorded in [`decisions.md`](decisions.md). Implementation status: section 11.

## 1. Summary

JARVIS is a small platform, not a single agent. It is cut into four security domains:

- **core**: task router, orchestrator, model router, boundary policy, audit, self-improvement
- **trading**: market analysis, backtests, paper and live trading
- **content**: research, content creation, reel ingestion
- **private**: Life-OS (calendar, email, notes, health, time tracking)

Each domain has its own Hermes instance (supervisor and workers), its own n8n instance, its own databases, its own secrets and its own network. Domains never talk to each other directly; they exchange typed, classified events through core, which checks every crossing.

### Guiding principles

1. **Isolation through infrastructure, not prompts.** What an agent cannot reach, it cannot leak. Every boundary is a network, a database role or a missing credential.
2. **Data only flows into stricter zones.** Data may move into a zone of equal or higher classification (public to private), never the other way without an explicit, deterministic declassification step.
3. **LLMs propose, deterministic code decides.** Every action with an external effect (order, post, email, calendar entry, merge) passes a non-LLM gate and, depending on risk, human approval.
4. **No agent ever sees a secret.** Credentials live in SOPS-encrypted files and are read only by deterministic executors (n8n, risk gate, git gateway, model router), never by an agent.
5. **Everything is traceable.** Every decision, loop iteration and action produces an immutable, hash-chained audit entry, without private content ending up in the audit log.
6. **Self-improvement cannot touch its own guardrails.** Gates, routing policy, boundary policy, audit and the risk gate are protected paths that only a human changes.

## 2. System overview

```text
          External world: Telegram · Instagram · CalDAV/IMAP · Broker APIs · Model APIs
                                             │
                    ┌────────────────────────▼─────────────────────────┐
                    │ net-edge: Caddy (mTLS) + Tailscale, no open ports │
                    └────────────────────────┬─────────────────────────┘
                    ┌────────────────────────▼─────────────────────────┐
                    │ net-gateway: API gateway, UI                      │
                    └──┬─────────────┬──────────────┬─────────────┬────┘
          ┌────────────▼───┐ ┌───────▼───────┐ ┌────▼─────────┐ ┌─▼─────────────┐
          │ CORE           │ │ TRADING       │ │ CONTENT      │ │ PRIVATE       │
          │ task router    │ │ Hermes        │ │ Hermes       │ │ Hermes        │
          │ orchestrator   │ │ n8n-trading   │ │ n8n-content  │ │ n8n-private   │
          │ model router   │ │ risk gate     │ │ pg-content   │ │ pg-private    │
          │ audit writer   │ │ pg-trading    │ │ qdrant       │ │ qdrant, redis │
          │ pg-core/audit  │ │               │ │              │ │ local only    │
          └───────┬────────┘ └───────────────┘ └──────────────┘ └───────────────┘
                  │ (only the model router)
          ┌───────▼────────────────────────┐
          │ net-inference: Ollama, one GPU │   Sandboxes: net-sandbox-<id>, per run
          └────────────────────────────────┘
```

Events enter only through a domain's n8n instance or the API gateway. Inside each domain, agents reach their n8n tools only through the domain's policy gateway (Envoy + OPA), and only egress proxies have a route out (section 7.3). Core issues work to domains as signed task tokens and receives results and metadata back. Only deterministic executors hold credentials. The private domain reports nothing but metadata.

## 3. Components

### 3.1 Core

| Component | Responsibility | Technology | Must never |
|---|---|---|---|
| Task router | Accepts goals, picks the domain, issues signed task tokens (task, domain, allowed tools, budget, taint, expiry) | Python, no data access | Read domain data |
| Orchestrator | Owns each task's state machine (section 4), retries, escalation | Python, state in `pg-core`, queue in Redis Streams | Call models itself |
| Model router | Picks the model per call by caller domain, classification and budget; PII scanner; per-domain semantic cache; FinOps circuit breaker | LiteLLM plus the JARVIS policy layer | Forward PRIVATE content or any private-domain call to a cloud endpoint (fail closed) |
| Boundary policy | Decides every cross-domain event; later enforced by a policy gateway (OPA behind Envoy) in front of all MCP servers | Python rules now, Rego later | Be modified by agents |
| Audit writer | The only writer of the audit log; hash chain; redaction | Python, `pg-audit` | UPDATE or DELETE |
| Git gateway | The only way agents reach git; GitHub App token, `auto-improve/*` branches only | Python | Push to `main` or change protected paths |
| Self-improvement | Nightly audit of metadata, proposes changes as PRs from a sandbox | Hermes worker in core | Touch protected paths |
| API gateway | Single entry point for the dashboard and mobile clients, auth, WebSocket telemetry | FastAPI, Pydantic/Zod at the boundary | Answer without device and user identity |

### 3.2 Domains

Every domain is built from the same parts, so new domains can be scaffolded with `/scaffold-module`:

1. **Hermes instance** with its own supervisor (Tier 1) and workers (Tier 2), its own data directory and its own skills (`skills/<domain>/`). Max 4 iterations per sub-task.
2. **Deterministic gate** (Tier 0b): Pydantic schemas, linters, tests, business rules.
3. **n8n instance** (Tier 0a) with its own encryption key and credentials: inbound webhooks and polling, normalisation and labelling, outbound actions as MCP triggers.
4. **Stores**: own Postgres instance, own Qdrant instance where needed, own cache namespace. Agents read curated views through a domain MCP server, never raw tables.
5. **Action gate**: the last check before any external effect.

| Domain | Workers | Supervisor | External execution | Specifics |
|---|---|---|---|---|
| Trading | Local (analysis, backtests) | Cloud with pseudonymised data for strategy reviews; local for anything account-related | Risk gate + `BrokerAdapter` with its own broker keys | Non-LLM risk gate: position size, daily loss, drawdown, instrument allowlist, order frequency, kill switch; paper/testnet first |
| Content | Cloud (research, text), local for media and reel extraction | Cloud | `n8n-content` (social APIs) | Publishing always needs approval; reel content runs in a quarantined worker without tools |
| Private | Local only | Local, inside the domain | `n8n-private` (CalDAV, IMAP) | No cloud route exists; egress only to CalDAV and IMAP hosts |
| Core | Local (self-improvement) | Cloud for reviews | Git gateway | Sandboxed; protected paths immutable |

### 3.3 Shared execution services

- **Ollama** in `net-inference`, reachable only by the model router. One GPU (RTX 4070 Ti, 12 GB VRAM), `OLLAMA_MAX_LOADED_MODELS=1`. Models in Q4: `qwen2.5-coder:14b` up to about 8k context (else `:7b`), `deepseek-r1:8b`, `qwen2.5vl:7b`; `bge-m3` embeddings on the CPU; Faster-Whisper as a batch job while no LLM is loaded. Larger coding tasks without private data go to a cloud worker. A dedicated private-domain instance follows once a second GPU exists.
- **Sandboxes**: ephemeral rootless containers (gVisor or rootless Podman), read-only root filesystem, `--cap-drop=ALL`, no network or a per-run egress allowlist; for code execution, `yt-dlp`, browser automation and backtests.

## 4. Supervisor-worker loop

Each task is a state machine in the orchestrator (`core/jarvis_core/orchestrator/state_machine.py`), not a free-form agent conversation.

```text
PLANNED → RUNNING → GATE_CHECK  fail → RUNNING (error trace, no supervisor call)
                                pass → SUPERVISOR_QA  reject → RUNNING (feedback)
                                                      accept → DONE
                                                      accept with action → AWAITING_ACTION
AWAITING_ACTION  approved → EXECUTING → DONE
                 rejected → ESCALATED
More than 4 retries, or budget exhausted → ESCALATED (human decides in the dashboard)
```

Rules:

- **Acceptance schema up front.** The supervisor fixes the Pydantic schema and tests a result must pass; Tier 0b checks exactly that.
- **Workers have no action tools.** An intended external effect becomes an `ActionRequest`, decided by the action gate (`core/jarvis_core/gate/`).
- **Task tokens scope every worker.** A tool call outside the token's allowed tools is rejected. A token carries a `taint` once external content is in context; tainted tasks need human approval for any write.
- **Budget per task and per domain.** The circuit breaker applies globally (2.50 EUR/h, 15 EUR/day) and per domain; the private domain has a cloud budget of zero.
- **Every transition is audited.**

## 5. Domains and boundaries

### 5.1 Data classification

Every data object and event carries the **origin domain** (taken from the caller's workload identity, never from a field an agent sets) and a **class**. Labels are set at the source and can only be changed by deterministic code.

| Class | Examples | Allowed models | Storage | Dashboard |
|---|---|---|---|---|
| `PUBLIC` | Reels, market data, published posts, research | Local and cloud | Any domain | Content visible |
| `INTERNAL` | Drafts, strategy descriptions, skills, code | Local and cloud | Own domain | Content visible |
| `CONFIDENTIAL` | Positions, balances, orders, platform accounts | Local; cloud only after pseudonymisation | Own domain, encrypted | Aggregated |
| `PRIVATE` | Calendar, email, health, journal, contacts | Local only | Private domain only | Metadata only |
| `SECRET` | API keys, OAuth tokens, broker keys | No model | SOPS files only | Never |

Missing or unknown labels are treated as `PRIVATE`. Anything originating in the private domain is at least `PRIVATE`.

### 5.2 Boundary rules

1. **No direct connection between domains.** Networks, database instances and credentials are separate.
2. **Upgrading is allowed, downgrading is not.** A public reel may become a proposal in the private inbox; private content never leaves the private domain.
3. **Crossings are typed events, checked by core** (`core/jarvis_core/policy/flows.py`). No free text or prompts cross domains.
4. **Proposals, not writes.** Content can only place proposals in the trading and private inboxes; the target domain decides.
5. **Fail closed.**
6. **External content is data, not instructions.** Reels, emails and web pages are read by a quarantined worker without tools that returns only a typed object.

### 5.3 Allowed flows

| From → to | Allowed | Form |
|---|---|---|
| Core → any domain | Yes | Signed task token |
| Any domain → core | Yes | Result object, audit entry, metadata-only telemetry |
| Content → trading | As proposal | `TradingIdeaProposed` into the trading inbox |
| Content → private | As proposal | `LifeTipProposed` into the private inbox |
| Private → anywhere except core metadata | No | Only by single, step-up approved exception |
| Trading ↔ content | No | No use case |

## 6. Key data flows

### 6.1 Inbound event to action (base pattern)

1. An external event hits the domain's n8n instance, which verifies it, normalises it and sets the class.
2. Core's task router issues a task token to the domain's supervisor, which plans the DAG.
3. Workers execute; Tier 0b checks every result; the supervisor checks semantics.
4. An intended external effect becomes an `ActionRequest`; the action gate decides, with human approval where required.
5. The domain's executor (n8n workflow or risk gate) acts with its own credentials.
6. Every step writes an audit entry; metadata-only telemetry goes to the dashboard.

### 6.2 Reel to knowledge (content domain)

1. A reel URL arrives via Telegram in `n8n-content`.
2. A sandbox downloads it with `yt-dlp`, egress limited to the Instagram CDN.
3. Locally, Faster-Whisper transcribes and the vision model reads keyframes.
4. A quarantined worker without tools builds a `ReelKnowledgeArtifact`; Tier 0b validates it. It carries the `external_content` taint.
5. Depending on its category it becomes a content template, or a proposal in the trading or private inbox. Skill candidates go to core's self-improvement flow as a PR.

### 6.3 Trading idea to order

1. A proposal lands in the trading inbox; trading decides whether to pursue it.
2. Workers express a strategy as code; Tier 0b checks types, tests and forbidden constructs.
3. Backtests run in a sandbox without broker access.
4. Passing review means paper/testnet trading, not real money. Going live is a human step-up decision per strategy with a capital limit.
5. Live signals pass the risk gate (plain code). Only passing signals reach the `BrokerAdapter`.
6. The kill switch stops all orders and is independent of the LLM path.

No LLM ever places an order or knows broker credentials.

### 6.4 Private domain (fully local)

1. `n8n-private` fetches calendar and email into `pg-private`, class `PRIVATE`.
2. The local supervisor and workers plan, summarise and remind. The model router has no cloud route for this domain.
3. Actions pass the action gate; emails are drafts by default.
4. Only metadata goes to core.

### 6.5 Self-improvement (core)

1. Nightly the self-audit reads audit metadata: high-retry tasks, escalations, cost outliers, Tier 0 failure patterns. No private content.
2. A worker proposes changes in a sandboxed git worktree; the full test suite runs there.
3. The git gateway pushes `auto-improve/<date>-<topic>` and opens a PR; changes to protected paths are rejected.
4. A human merges.

## 7. Zero-trust security

The full concept, including the threat model, lives in the project's Zero-Trust security document. This section summarises what the code and infrastructure enforce.

### 7.1 Identity

- Workload identities per domain and role (SPIRE or step-ca, short-lived mTLS certificates) arrive in a later phase. Until then, the domain is fixed per container by configuration and network.
- Signed task tokens scope each worker to one task (`core/jarvis_core/contracts/task_token.py`).
- Humans sign in to the dashboard with a passkey over Tailscale only. Approvals for orders, publishing, self-modification and new n8n workflows need a fresh passkey confirmation.

### 7.2 Secrets

- SOPS with one age key per domain; encrypted `secrets/<domain>.sops.yaml` files in git; private age keys only on the server (`/etc/jarvis/age/<domain>.key`) plus an offline copy.
- `scripts/secrets-unpack.sh` decrypts each domain's file to tmpfs; each container mounts only its own domain's files.
- Code reads secrets only through `SecretProvider`, which refuses other domains' paths. Vault can replace SOPS behind the same interface.
- No GitHub personal access token: a GitHub App limited to this repository and `auto-improve/*`.
- Broker keys: trade-only, no withdrawal, IP allowlist; paper and live keys separate.

### 7.3 Network

| Network | Members | Egress |
|---|---|---|
| `net-edge` | Caddy, Tailscale sidecar | Tailnet only |
| `net-gateway` | API gateway, UI backend | None |
| `net-core` | Task router, orchestrator, model router, audit writer, `pg-core`, `pg-audit`, `redis-core`, `egress-core` | Cloud LLM APIs and GitHub via `egress-core` |
| `net-trading` | Trading Hermes, risk gate, `pg-trading`, `mcp-gateway-trading` | None |
| `net-content` | Content Hermes, `pg-content`, `qdrant-content`, `mcp-gateway-content` | None |
| `net-private` | Private Hermes, `pg-private`, `qdrant-private`, `redis-private`, `mcp-gateway-private` | None |
| `net-mcp-<domain>` | The domain's n8n, OPA, policy gateway and egress proxy | Per-domain allowlist via `egress-<domain>`; private: CalDAV and IMAP only via `egress-private` |
| `net-private-assistant` | `private-assistant`, `egress-private` | CalDAV and IMAP only via `egress-private` |
| `net-inference` | Ollama | None |
| `net-inference-private` | `private-assistant`, Ollama | None |
| `jarvis-sandbox-egress` | Reel fetch sandboxes, `egress-reel` | Instagram hosts via `egress-reel` |
| `net-sandbox-<id>` | One sandbox run | Per run, default none |
| `net-egress` | `egress-*` proxies only | Internet |

Agents never reach their n8n directly: the policy gateway (Envoy with OPA as ext_authz, `policies/mcp/`) is the only member of both `net-<domain>` and `net-mcp-<domain>`. Allowlists live in `policies/egress/`. Runbook: [`runbooks/isolation-services.md`](runbooks/isolation-services.md).

All networks except `net-egress` are `internal: true`. No host ports; debugging uses `docker/docker-compose.debug.yml` on a workstation only.

### 7.4 Supply chain

Images and MCP servers pinned to versions (digests via Renovate), SBOM per build, gitleaks in pre-commit and CI.

### 7.5 Protected paths

Only a human changes these. CODEOWNERS requires owner review; the git gateway rejects agent changes:

`core/jarvis_core/policy/`, `core/jarvis_core/gate/`, `core/jarvis_core/router/policy.py`, `core/jarvis_core/audit/`, `modules/mod-trading/risk/`, `policies/`, `docker/`, `secrets/`, `.github/`, `.claude/`, `CLAUDE.md`

### 7.6 Approval tiers

| Tier | Examples | Approval |
|---|---|---|
| A: read-only, internal | Research, summary, backtest | Automatic |
| B: reversible, private | Draft, branch | Automatic; human if the task is tainted |
| C: externally visible | Publish, send email, paper order | Human |
| D: money or code into `main` | Live order outside an approved strategy, strategy go-live, merge | Human with step-up |

## 8. Persistence and audit

| Store | Content | Isolation |
|---|---|---|
| `pg-core` | Tasks, states, approvals, budgets | `net-core` |
| `pg-audit` | Audit chain | `net-core`; writer may only call `audit.append()` and `audit.head()` |
| `pg-trading`, `pg-content`, `pg-private` | Domain data | One instance per domain; role `app` for executors, `agent_ro` sees only `agent_views` |
| `qdrant-content`, `qdrant-private` | Vectors | One instance per domain that needs one |
| `redis-core`, `redis-private` | Bus, queues, budget counters; private-internal queues | Separate instances |

The semantic cache is per domain and never stores `CONFIDENTIAL`, `PRIVATE` or `SECRET`; the private domain has no cache.

**Audit chain.** `entry_hash = SHA-256(prev_hash || canonical payload)`. The database re-checks every link inside `audit.append()` and blocks UPDATE, DELETE and TRUNCATE for every role. Entries hold hashes and references, never content; private-domain entries hold metadata only. Later phases add an hourly signature of the chain head and a daily WORM export.

**Retention.** Trading decisions 5 years; agent decisions at least 6 months; private-domain metadata 12 months; telemetry 14 days.

## 9. Command dashboard

The dashboard observes and approves; it is not a privileged control channel. It talks only to the API gateway. Telemetry events carry task id, domain, node, state, duration, cost, model class and error class, never prompts, outputs or raw data. Views: live DAG, approval deck, FinOps, trading cockpit with kill switch, audit explorer, private view (metadata only by default). Details are in the dashboard concept.

## 10. Deployment and operations

- **Workstation**: `scripts/secrets-unpack.sh`, then `docker compose -f docker/docker-compose.yml up -d`.
- **Homelab**: identical stack, Tailscale and Caddy only, server in its own VLAN.
- **Observability**: OpenTelemetry traces, Prometheus metrics, Loki logs without content.
- **Alerts** via Telegram: policy denials, private-to-cloud attempts, risk gate rejections, chain breaks, budget trips, break-glass use.
- **Emergency stops**: global pause, trading kill switch, revoke a domain's secrets.

## 11. Roadmap

Each phase ends with an exit criterion that must hold before the next starts.

1. **Foundation** (done, PR #2): Python workspace and CI; contracts (domains, classes, events, audit entries, action requests, task tokens); boundary policy; action gate; model router policy with PII scanner, cache namespaces and circuit breaker; orchestrator state machine; audit chain with append-only Postgres schema; SOPS secrets with one age key per domain; Compose with one database instance and one internal network per domain.
   Exit: tests prove that no private-domain or `PRIVATE` request reaches a cloud endpoint, that each data service sits only in its own domain network with only its own secrets, and that the audit log is append-only.
2. **First vertical slice: reel pipeline** in the content domain with quarantine worker, sandbox and local models (implemented in `modules/mod-content/`; runbook: [`runbooks/reel-pipeline.md`](runbooks/reel-pipeline.md)). Model calls go through the inference gateway (`core/jarvis_core/router/gateway.py`), which combines routing policy, circuit breaker and audit.
   Exit: 20 real reels processed, cost and audit visible (`python -m mod_content.reel report`). Open: needs a run on the workstation GPU.
3. **Dashboard skeleton** and API gateway with passkey login (implemented in `apps/deck/` and `core/jarvis_core/api/`; runbook: [`runbooks/command-deck.md`](runbooks/command-deck.md)). Approvals are passkey ceremonies bound to one request and one decision; executors accept only the resulting single-use grant (`core/jarvis_core/approvals/`).
   Exit: a tier C action can only be approved via the deck (`tests/api/test_deck_approvals.py`).
4. **Isolation services**: one n8n instance per domain, policy gateway (OPA behind Envoy) in front of all MCP servers, egress proxy with per-domain allowlists, git gateway (implemented in `docker/`, `policies/` and `core/jarvis_core/gitgateway/`; runbook: [`runbooks/isolation-services.md`](runbooks/isolation-services.md)).
   Exit: live network tests in Docker show that agents have no route out and reach tools only through the policy gateway, that each egress proxy allows only its allowlist, and that the git gateway refuses protected paths (`tests/isolation/`, `tests/gitgateway/`).
5. **Private domain** with local supervisor (implemented in `modules/mod-private/`; runbook: [`runbooks/private-domain.md`](runbooks/private-domain.md)): calendar and mail sync, triage, daily briefing and reply drafts on local models, in a container that reaches only CalDAV, IMAP and Ollama.
   Exit: a network test shows the private domain reaches only CalDAV and IMAP (`tests/isolation/test_private_live.py`, `test_isolation_live.py`).
6. **Trading domain** with `BrokerAdapter`, risk gate and kill switch; paper/testnet only (implemented in `modules/mod-trading/`, risk engine in `modules/mod-trading/risk/`; runbook: [`runbooks/trading-paper.md`](runbooks/trading-paper.md)). Orders pass the action gate, the non-LLM risk engine and a fail-closed kill switch before the paper broker; backtests use the same path.
   Exit: several weeks of paper trading without risk-limit violations before a live key exists. Open: needs a market-data feed through `n8n-trading`.
7. **Self-improvement** and operations hardening (SPIRE, WORM export, chain signatures).
   Exit: a deliberate auto-PR against the risk gate is rejected.
