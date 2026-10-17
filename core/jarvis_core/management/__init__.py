"""Core's management layer: agents that run the system rather than a domain's work.

- `delegation`: turns an owner's request in plain words into a typed goal for the one
  specialised domain agent that may do it, then hands it to the orchestrator.
- `improvement`: learns from how tasks end (metadata only) and keeps lessons.
- `memory`: the shared memory port the management agents read and write.

Models propose; deterministic code checks every proposal (Tier 0b) and decides.
"""
