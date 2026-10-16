# Secrets

Encrypted with SOPS, one age key per domain (`core`, `trading`, `content`, `private`).
Only the encrypted `<domain>.sops.yaml` files belong in git. Private age keys live on the
server in `/etc/jarvis/age/<domain>.key` (root-owned) plus an offline copy.

```bash
scripts/secrets-bootstrap.sh   # once: create age keys and encrypted files with random values
scripts/secrets-unpack.sh      # before `docker compose up`: decrypt into /run/jarvis/secrets
sops secrets/trading.sops.yaml # edit a domain's secrets (needs SOPS_AGE_KEY_FILE for it)
```

A container only receives files from its own domain directory. Agents never receive secrets;
only deterministic executors read them through `jarvis_core.secrets.SecretProvider`.
The provider can later be replaced by HashiCorp Vault without changing its callers.
