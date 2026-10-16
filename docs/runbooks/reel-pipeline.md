# Runbook: reel pipeline (Phase 2)

Turns Instagram reel URLs into validated `ReelKnowledgeArtifact`s in the content domain, fully on the local GPU, and reports cost and audit coverage. Design: [`architecture.md`](../architecture.md) section 6.2.

## What runs where

| Step | Where | Network | Notes |
|---|---|---|---|
| fetch | `jarvis-reel-media` sandbox (yt-dlp) | `jarvis-sandbox-egress` only, through `egress-reel` and [`policies/egress/reel.allow`](../../policies/egress/reel.allow) | max 200 MB, max 10 min, no playlists, no cookies or accounts |
| extract | `jarvis-reel-media` sandbox (ffmpeg) | none | 16 kHz mono WAV, up to 12 scene-change keyframes, metadata.json |
| transcribe | `jarvis-reel-whisper` sandbox (Faster-Whisper) | none | GPU; Ollama models are unloaded first (one GPU, one model) |
| keyframe reading | Ollama `qwen2.5vl:7b` via the inference gateway | – | local only (`require_local`) |
| extraction | Ollama `deepseek-r1:8b` via the inference gateway | – | quarantined worker without tools, JSON schema output, max 4 iterations |
| Tier 0b, QA, follow-ups | host process | – | deterministic gate, rule supervisor, boundary policy, action gate |

Every sandbox runs read-only, as `nobody`, with all capabilities dropped, `no-new-privileges`, memory, CPU and PID limits, and exactly one writable directory. Sandbox images are never pulled at run time (`--pull=never`). The host process never parses media itself; it only checks file names, sizes and magic bytes of what the sandboxes wrote.

## One-time setup (workstation)

```bash
# 1. Sandbox images (rootless Podman recommended; Docker works with --engine docker)
podman build -t localhost/jarvis-reel-media:0.1.0   -f modules/mod-content/sandbox/Dockerfile.media   modules/mod-content/sandbox
podman build -t localhost/jarvis-reel-whisper:0.1.0 -f modules/mod-content/sandbox/Dockerfile.whisper modules/mod-content/sandbox

# 2. Network and egress proxy for the download step (compose creates jarvis-sandbox-egress)
docker compose -f docker/docker-compose.yml up -d egress-reel
# Run the CLI with --engine docker so the sandbox joins that network.

# 3. Whisper model, downloaded once and mounted read-only afterwards
sudo mkdir -p /var/lib/jarvis/models/whisper /var/lib/jarvis/content
uvx --from huggingface_hub hf download Systran/faster-whisper-large-v3 \
    --local-dir /var/lib/jarvis/models/whisper/large-v3

# 4. Local models in Ollama (see docker/docker-compose.models.yml)
docker exec jarvis-ollama-1 ollama pull qwen2.5vl:7b
docker exec jarvis-ollama-1 ollama pull deepseek-r1:8b

# 5. Ollama on loopback for the workstation run (debug override, never on the server)
docker compose -f docker/docker-compose.yml -f docker/docker-compose.debug.yml up -d ollama
```

The GPU must be visible to the sandbox: with Podman via the NVIDIA Container Toolkit CDI spec (`nvidia-ctk cdi generate`), with Docker via `--gpus`.

## Processing reels

```bash
uv run python -m mod_content.reel ingest https://www.instagram.com/reel/<code>/
uv run python -m mod_content.reel ingest --from-file reels.txt     # one URL per line, # comments
uv run python -m mod_content.reel report
```

Data lives in `/var/lib/jarvis/content` (override with `--data-dir` or `JARVIS_CONTENT_DATA`):

- `runs.jsonl`: one record per run with state, iterations, stage durations, model calls, tokens, cost and the artifact
- `audit.jsonl`: the hash-chained audit log for this workstation run; it is re-verified every time it is opened
- `outbox.jsonl`: typed proposals (`TradingIdeaProposed`, `LifeTipProposed`) and `TaskMetadata`, already checked by the boundary policy, waiting for the target domains (Phases 5 and 6)

Downloaded media and audio are deleted after each run unless `--keep-media` is set.

## Outcomes

| State | Meaning | Who acts |
|---|---|---|
| `done` | Artifact stored; proposals queued if any | Nobody |
| `awaiting_action` | Injection patterns were found, or a skill candidate needs approval | You, in the approval deck (Phase 3); until then read `runs.jsonl` |
| `escalated` | A sandbox step failed, media was rejected, a model was unavailable, or the worker failed 4 times | You; `error_class` says which |

## Exit criterion

`report` prints `exit_criterion_met: true` once **20 distinct reels** reached `done` or `awaiting_action` **and** the audit chain verifies. Cost per reel is reported too; with local models it is 0 EUR in API cost.

## Known limits of Phase 2

- **Egress** is enforced since Phase 4: `jarvis-sandbox-egress` is internal, and its only way out is `egress-reel`, which allows HTTPS to the Instagram hosts in `policies/egress/reel.allow` and nothing else.
- **Supervisor QA is rule-based.** The content supervisor is meant to be a cloud model; that needs the cloud client and keys. The `Supervisor` interface is in place.
- **Task tokens are issued by the CLI itself** with an ephemeral key, because the core task router is not a service yet. Every step is still checked against the token.
- **Telegram intake** goes through `n8n-content` (Phase 4). Its egress to `api.telegram.org` is allowed, but a workflow that hands URLs to the pipeline does not exist yet; until then URLs are passed to the CLI.
- Instagram changes its pages often. When downloads start failing with `fetch_failed`, rebuild the media image with a current `YTDLP_VERSION`.
