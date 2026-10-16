# Architecture Decisions

Accepted by the project owner on 2026-10-16 and aligned with the Zero-Trust security concept the same day. Where the two documents differed, the stricter option was taken. Context: [`architecture.md`](architecture.md).

| # | Question | Decision | Revisit when |
|---|---|---|---|
| 1 | System name | **JARVIS** (replaces "Nexus-OS" everywhere) | – |
| 2 | Domains | **core, trading, content, private**; reel ingestion belongs to content, self-improvement to core | A new domain is needed |
| 3 | Hermes instances | **One per domain**, each with its own supervisor and data directory; core runs a thin task router that issues signed task tokens and has no data access | – |
| 4 | Local inference | **One Ollama** on the RTX 4070 Ti (12 GB), in `net-inference`, reachable only via the model router, one model loaded at a time. Q4 models: `qwen2.5-coder:14b` (to ~8k context, else `:7b`), `deepseek-r1:8b`, `qwen2.5vl:7b`; `bge-m3` on the CPU | A second or larger GPU arrives |
| 5 | n8n instances | **One per domain** (`n8n-trading`, `n8n-content`, `n8n-private`), each with its own encryption key; agents cannot use the n8n REST API | – |
| 6 | Secrets | **SOPS + age, one key per domain**, behind `SecretProvider` | Dynamic DB credentials, a second server or a second person are needed: then HashiCorp Vault |
| 7 | Policy | **Python boundary rules now**, OPA (Rego) behind Envoy as policy gateway in front of MCP servers (Phase 4, `policies/mcp/`) | – |
| 8 | Event bus | **Redis Streams** | A second host joins: then NATS |
| 9 | Trading supervisor | **Cloud with pseudonymised data** for strategy reviews, **local** for anything account-related | – |
| 10 | Broker | **Broker-independent `BrokerAdapter`** (`get_quote`, `get_positions`, `place_order`, `cancel_order`). Must-haves: no withdrawal via API, IP allowlist, paper/testnet with the same API, EU regulation. Candidates to verify: Interactive Brokers (stocks, ETFs), an EU-licensed crypto exchange such as Kraken via CCXT | Before the first live key |
| 11 | Private calendar and mail access | **Read directly by the private assistant's deterministic executor** (CalDAV `REPORT`, IMAP `EXAMINE`/`BODY.PEEK`), not through an n8n MCP tool: n8n has no on-demand IMAP read node. Credentials via `SecretProvider`, egress only to the two hosts via `egress-private`. Accepted 2026-10-16 (Phase 5) | n8n gains an on-demand IMAP read, or the private domain needs more external services |

## Corrections to the original context files

The original context (`JARVIS_context/`) is superseded where it conflicts with these decisions:

- Shared Postgres, Qdrant, Redis and semantic cache are replaced by per-domain instances and namespaces.
- The default database password and credentials in MCP configuration are removed; agents read curated views through domain MCP servers.
- The GitHub personal access token is replaced by a GitHub App behind the git gateway.
- Browser automation runs only in sandboxes; its output is tainted.
- Telemetry carries metadata only; the audit log stores hashes and references, not content.
- Self-improvement agents may not edit protected paths.
- Images and MCP servers are pinned instead of `latest` / unpinned `npx -y`.
- Model sizes follow decision 4 (the original `qwen2.5-coder:32b` does not fit 12 GB).
