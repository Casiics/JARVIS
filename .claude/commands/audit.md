---
description: Audits the repository for domain isolation, privacy, EU AI Act traceability, gate coverage and code quality.
---

Audit the repository against `CLAUDE.md` and `docs/architecture.md`:

1. **Domain isolation:** no module imports from, calls, or reads the store of another domain; cross-domain data uses typed boundary events checked by `jarvis_core.policy.flows`.
2. **Privacy:** nothing from the `private` domain or labelled `PRIVATE` can reach a non-local model endpoint, a shared cache, telemetry or logs.
3. **Secrets:** no credentials in code, MCP configs, prompts or agent containers.
4. **Traceability:** every external action and supervisor evaluation emits an `AuditLogEntry` without content.
5. **Gate coverage:** every worker loop runs Tier-0b validation before any Tier-1 call; every external effect goes through the action gate; trading orders pass the risk engine.
6. **Code quality:** English comments only, no TypeScript `any`, complete Python type annotations.

Output a report with severities `CRITICAL`, `WARNING`, `OPTIMIZATION`. Fix `CRITICAL` and `WARNING` findings on a separate branch, except in protected paths, where you list the proposed change for the user instead.
