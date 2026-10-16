# JARVIS

Personal multi-agent platform built on Hermes Agent (Nous Research) as the core runtime, with a supervisor-worker loop, n8n as the deterministic I/O bus, and a real-time command dashboard.

Three domains share one platform but never share data, credentials or memory: **Trading**, **Content** and **Life-OS** (private life data), plus a **Platform** cell for reel ingestion and self-improvement.

- Target architecture: [`docs/architecture.md`](docs/architecture.md)
- Architecture decisions: [`docs/decisions.md`](docs/decisions.md)
- Rules for coding agents: [`CLAUDE.md`](CLAUDE.md)

Status: architecture defined, implementation not started. See the roadmap in the architecture document.
