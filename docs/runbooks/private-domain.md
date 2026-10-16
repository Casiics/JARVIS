# Runbook: private domain (Phase 5)

The private assistant reads your calendar and mailbox, sorts new mail, writes a daily briefing and drafts replies. Everything is `PRIVATE` and runs on local models only; nothing is sent. Design: [`architecture.md`](../architecture.md) sections 5, 6.4 and 7.3. Code: `modules/mod-private/`.

## What it does

| Command | What happens |
|---|---|
| `sync` | Reads calendar events from yesterday to 14 days ahead (one CalDAV `REPORT`) and mail above the last seen UID (IMAP `EXAMINE` and `BODY.PEEK`, so nothing is marked read). At most 50 mails and 2 MB per mail per run |
| `triage` | A local worker sorts each new mail (action needed, reply suggested, FYI, newsletter, spam or phishing) |
| `briefing [--date YYYY-MM-DD]` | A local worker writes the day's overview; Tier 0b checks it against the sources, then the local supervisor reviews it |
| `daily` | `sync`, `triage` and `briefing` in one go |
| `mail` | Lists the 30 newest mails with their id and category |
| `reply MAIL_ID "INSTRUCTION"` | A local worker drafts a reply. The draft waits in the private store for approval; it is never sent |
| `drafts` | Lists drafts and their status |

Each model step runs through the task state machine (at most 4 iterations, Tier 0b before the supervisor). Mail and calendar text reach the workers only wrapped as untrusted data, and the workers have no tools. The Tier 0b gate refuses invented event ids, wrong times, and phone numbers or addresses that are not in the sources.

## Where it runs

`private-assistant` is a one-shot container (profile `tools`, so `up` does not start it):

| Network | Reaches |
|---|---|
| `net-private-assistant` | `egress-private` at `10.213.41.2`, which passes TLS through to exactly your CalDAV host (443) and IMAP host (993) |
| `net-inference-private` | Ollama, nothing else |

It does not join `net-mcp-private`, so it cannot reach `n8n-private` around the policy gateway. The container runs as user 10001 with a read-only root and no capabilities. Its data (mail, events, triage, briefings, drafts, and a hash-chained audit file) lives in the volume `private_assistant_data` with mode 0700 and files 0600.

The CalDAV and IMAP credentials are read only by the assistant's deterministic code via `SecretProvider`. Models never see them; `tests/isolation/test_private_live.py` checks every request that reaches the model.

## Set up

1. Credentials. A fresh `scripts/secrets-bootstrap.sh` creates empty `caldav_username`, `caldav_password`, `imap_username` and `imap_password` in `secrets/private.sops.yaml`. On an existing install, add the four keys. Then fill them in:

   ```bash
   SOPS_AGE_KEY_FILE=/etc/jarvis/age/private.key sops secrets/private.sops.yaml
   sudo scripts/secrets-unpack.sh private
   ```

   Use an app password where your provider offers one. The assistant refuses to start with an empty credential.

2. Configuration (for example in `docker/.env`, which is not committed):

   ```bash
   JARVIS_CALDAV_HOST=cloud.example.org
   JARVIS_CALDAV_URL=https://cloud.example.org/remote.php/dav/calendars/<user>/personal/
   JARVIS_IMAP_HOST=imap.example.org
   JARVIS_OWNER_ADDRESS=you@example.org
   JARVIS_TZ=Europe/Berlin
   ```

   The CalDAV URL is the calendar collection itself, not the server root. IMAP uses port 993 with TLS.

3. Models: workers and supervisor both run `deepseek-r1:8b` (the local `REASONING` and `SUPERVISOR` models in `core/jarvis_core/router/catalog.py`). Pull it once: `docker compose -f docker/docker-compose.yml exec ollama ollama pull deepseek-r1:8b`.

4. Build and run:

   ```bash
   docker compose -f docker/docker-compose.yml up -d egress-private ollama
   docker compose -f docker/docker-compose.yml build private-assistant
   docker compose -f docker/docker-compose.yml run --rm private-assistant daily
   docker compose -f docker/docker-compose.yml run --rm private-assistant reply '<mail id>' 'Agree, Thursday works'
   ```

   To run `daily` every morning, add a systemd timer or cron entry on the host that runs the last-but-one line.

## Tests

- `tests/private/`: parsers, IMAP and CalDAV clients, Tier 0b checks and the assistant's loops against fakes.
- `tests/isolation/test_private_live.py` (`JARVIS_TEST_DOCKER=1`): the real image runs `daily` against stand-ins for CalDAV, IMAP and Ollama behind `egress-private`, with TLS verified against a throwaway CA. It also shows that the mailbox only sees read commands, that the state survives between runs, that an untrusted certificate is refused, and that the assistant's network reaches nothing but the two hosts and Ollama.

## Known limits

- **Reading without n8n.** The assistant reads CalDAV and IMAP itself instead of through an `n8n-private` MCP tool: n8n has no on-demand IMAP read node, and this path keeps the credentials in one deterministic process with an egress limited to two hosts. Accepted as [decision 11](../decisions.md); it differs from the target flow in architecture section 6.4.
- **Saving drafts to the mailbox is wired but not reachable yet.** `save_draft` checks a single-use approval grant and appends the draft to the IMAP Drafts folder (`\Draft \Seen`, never sent). Grants are HMAC-signed with core's `approval_grant_key`, which the private domain may not read, and the deck's approval queue does not span processes yet. Both arrive with the orchestrator service and Ed25519-signed grants. Until then, drafts stay in the private store and the unit tests cover the execution path.
- **Audit** goes to a local hash-chained file in the data volume until the audit writer runs as a service.
- **Recurring events** are marked as recurring but not expanded; only the first occurrence's time is known.
- **Triage** is checked by Tier 0b only; the supervisor reviews the briefing, which is built from the triage.
- Life tips from other domains arrive through `receive()` after the boundary policy, but no domain sends them yet.
