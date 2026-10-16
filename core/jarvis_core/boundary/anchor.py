"""Anchoring a domain's local audit chain in core's central log (AuditAnchor events).

Domain side: `anchor_event()` after writing to the local chain, sent through the mailbox.
Checking side: `matches_anchor()` tells whether a chain file still contains an anchored head
at the anchored position. A chain rewritten from some point on, truncated or replaced fails
for every anchor at or after that point.
"""

from __future__ import annotations

from collections.abc import Sequence

from jarvis_core.audit.chain import GENESIS_HASH, ChainedEntry
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import AuditAnchor


def anchor_event(origin: Domain, chain: str, entries: Sequence[ChainedEntry]) -> AuditAnchor:
    return AuditAnchor(
        origin_domain=origin,
        target_domain=Domain.CORE,
        classification=DataClassification.INTERNAL,
        chain=chain,
        entries=len(entries),
        head_hash=entries[-1].entry_hash if entries else GENESIS_HASH,
    )


def matches_anchor(entries: Sequence[ChainedEntry], count: int, head_hash: str) -> bool:
    if count == 0:
        return head_hash == GENESIS_HASH
    return len(entries) >= count and entries[count - 1].entry_hash == head_hash
