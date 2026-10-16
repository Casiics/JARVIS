# Architecture Decisions

Accepted by the project owner on 2026-10-16. Context and rationale: [`architecture.md`](architecture.md).

| # | Question | Decision | Rationale |
|---|---|---|---|
| 1 | System name | **JARVIS** (replaces "Nexus-OS" everywhere) | Matches the repository name |
| 2 | One Hermes instance or one per cell | **One per cell**, each with its own data directory | Memory and learned skills must not leak across domains |
| 3 | Local inference | **One shared Ollama**, stateless, no egress, reachable only via the router; a dedicated Life-OS instance once a second GPU exists | Single GPU today; routing through the router keeps classification, budget and audit enforced |
| 4 | n8n instances | **Two**: `n8n-life` and `n8n-public`; broker orders go through the trading executor, not n8n | Keeps private credentials out of the public instance; order execution belongs in deterministic code |
| 5 | Policy language | **Open Policy Agent (Rego)** | Widely used, testable, policies as code |
| 6 | Event bus | **Redis Streams** to start; move to NATS once a second host joins | Redis is already in the stack |
| 7 | Trading supervisor model | **Cloud Tier 1 with pseudonymised data** for strategy reviews; **local** for anything tied to an account | Strong reviews without exposing account data |
| 8 | Broker | **Open**. Requirements: paper trading account, trade-only keys without withdrawal rights, IP binding | Not yet chosen |

## Corrections to the original context files

The original context (`JARVIS_context/`) is superseded where it conflicts with these decisions:

- Shared Qdrant collection, shared semantic cache and a single Postgres role are replaced by per-cell stores and roles.
- Database credentials and the GitHub token no longer appear in MCP server configuration; agents reach data through cell memory services and git through the git gateway.
- Browser automation runs only inside the sandbox runner.
- Telemetry carries metadata only.
- The audit log stores hashes and references, not content (GDPR erasure via crypto-shredding).
- Self-improvement agents may not edit protected paths (see `architecture.md` section 7.5).
- Container images are pinned by digest instead of `latest`.
- Model names in the router configuration are to be updated to current models when the router is implemented.
