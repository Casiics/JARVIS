# Runbook: isolation services (Phase 4)

Phase 4 puts a checkpoint on every way out of a domain. Agents get no network route to the internet and no direct line to their domain's tools. Design: [`architecture.md`](../architecture.md) sections 3, 6.5 and 7.3.

## What runs where

| Service | Networks | Job |
|---|---|---|
| `egress-core`, `egress-trading`, `egress-content` | domain side + `net-egress` | Squid. HTTPS (`CONNECT` to 443) to the host names in `policies/egress/<domain>.allow`, nothing else |
| `egress-reel` | `jarvis-sandbox-egress` + `net-egress` | Squid for the reel fetch sandbox, `policies/egress/reel.allow` (Instagram only) |
| `egress-private` | `net-mcp-private` + `net-egress` | nginx TLS passthrough to exactly one CalDAV host (443) and one IMAP host (993). No SMTP |
| `n8n-trading`, `n8n-content`, `n8n-private` | `net-mcp-<domain>` | One n8n per domain with its own encryption key; MCP Server Trigger workflows are the domain's tools |
| `opa-<domain>` | `net-mcp-<domain>` | Decides every MCP request with `policies/mcp/authz.rego` and only its own domain's `policies/mcp-tools/<domain>.json` |
| `mcp-gateway-<domain>` | `net-<domain>` + `net-mcp-<domain>` | Envoy. The only bridge between agents and their n8n. Asks OPA first and fails closed |

`net-egress` is the only network that is not `internal`, and only the `egress-*` proxies join it. The proxies also refuse destinations that resolve to private, loopback or link-local addresses, and raw IP addresses.

## Start

```bash
# New keys: n8n_encryption_key for trading, content and private.
#   Fresh install: scripts/secrets-bootstrap.sh creates them.
#   Existing install: `sops secrets/<domain>.sops.yaml` and add n8n_encryption_key: "<openssl rand -hex 32>".
# Private domain: the two hosts n8n-private may reach.
export JARVIS_CALDAV_HOST=cloud.example.org JARVIS_IMAP_HOST=imap.example.org
sudo -E scripts/secrets-unpack.sh
docker compose -f docker/docker-compose.yml up -d
```

`10.213.40.0/24` is reserved for `net-mcp-private` (the private proxy sits at `.2`). If it collides with your LAN, change both places in `docker/docker-compose.yml`.

## Building workflows in n8n

The editors are reachable only on the workstation with the debug override:

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.debug.yml up -d n8n-content
# trading http://127.0.0.1:5681, content :5682, private :5683
```

1. Build the workflow, ending in an **MCP Server Trigger** node. Note its path (the part after `/mcp/`).
2. Add that path to `endpoints` and each tool name to `tools` in `policies/mcp-tools/<domain>.json`. This is a protected path, so it lands by PR with your review.
3. `docker compose restart opa-<domain>`.

Agents call `http://mcp-gateway-<domain>:8080/mcp/<path>`. Anything not listed, any other MCP method (resources, prompts, sampling), any query string and bodies over 64 KiB are refused. OPA decision logs keep method and tool name but mask arguments.

n8n hardening: public API, telemetry, templates and community packages are off; nodes cannot read environment variables; the Execute Command, SSH, Git and local file nodes are removed; executions are pruned after 14 days. Credentials live in each n8n's own encrypted store.

**Not yet possible:** inbound webhooks (for example the Telegram trigger) need a public URL; they arrive with the edge setup. Until then, use polling triggers.

## Adding an egress host

Add the host name to `policies/egress/<domain>.allow` (a leading dot also allows subdomains), open a PR, then `docker compose restart egress-<domain>`. `tests/config/test_policies.py` rejects IP addresses and overlapping entries.

## Git gateway

`python -m jarvis_core.gitgateway` (library: `core/jarvis_core/gitgateway/`) is how self-improvement changes reach GitHub:

- Clones `main` fresh, applies the patch with `git apply --index`, and lists changes with renames split into delete and add.
- Refuses any change under the protected paths from `CLAUDE.md`, its own code, `.gitmodules`, `.gitattributes`, symlinks, submodules, mode changes, paths with `..`, and more than 50 files.
- Commits as `jarvis-git-gateway`, pushes only `auto-improve/<UTC timestamp>-<topic>` (never force), and opens a draft PR. Hooks never run.
- Authenticates as a GitHub App: a JWT signed with the App key, exchanged for an installation token limited to this repository with `contents` and `pull_requests` write. The token reaches git only as an http header for `github.com`.
- Audits every proposal (allowed or refused) as `self_modification` with the patch hash.

Setup, once:

1. Create a GitHub App on your account with repository permissions **Contents: write** and **Pull requests: write**, and install it on this repository only.
2. Add `github_app_id`, `github_app_installation_id` and `github_app_private_key` (the PEM) to `secrets/core.sops.yaml`.
3. Add a repository ruleset that lets the App create and update only `auto-improve/**` branches. GitHub App permissions cannot be limited to branch names, so the ruleset is what enforces it on GitHub's side.

## Verify

```bash
JARVIS_TEST_DOCKER=1 uv run pytest -v -m docker tests/isolation
```

starts the stack with stand-ins for the internet and for an MCP server and checks, from each position in the network:

- Agents have no route out and cannot reach any proxy, n8n or OPA.
- n8n reaches only its allowlist, only through its proxy, and only over HTTPS.
- The reel sandbox reaches only Instagram hosts.
- The private domain reaches only its CalDAV and IMAP host, with TLS end to end.
- The gateway forwards listed tools, refuses everything else, keeps arguments out of the logs, and fails closed without OPA.
- A real n8n MCP Server Trigger workflow (`tests/isolation/n8n-mcp-workflow.json`) answers a listed tool call through the gateway, while unlisted tools never reach it.

Known gap: `tools/list` returns every tool the n8n workflow exposes, including ones the domain's tool list does not allow. Calling them is refused; hiding them needs response filtering and comes later.

CI runs the same tests in the `isolation` job and the Rego tests in `policies`.
