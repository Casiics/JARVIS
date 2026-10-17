"""Which data may cross which domain boundary.

Rules (docs/architecture.md, section 5):
- Cross-domain data moves only as typed boundary events.
- Nothing leaves the private domain.
- Content may only propose into the trading and private inboxes.
- Every domain may report metadata to core: task status and the head of its local audit
  chain (AuditAnchor). Core may send tasks to every domain. Today the only task type is a
  reel the owner submitted in the deck (core -> content).
- Content may ask the owner to review a curation decision in the deck (content -> core), and
  the owner's answer goes back (core -> content). Only PUBLIC or INTERNAL material may be
  sent for review, because it is shown in core's deck.
- SECRET never crosses any boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import (
    AuditAnchor,
    BoundaryEvent,
    ContextReviewDecided,
    ContextReviewRequested,
    LifeTipProposed,
    ReelSubmitted,
    TaskMetadata,
    TradingIdeaProposed,
)

_ALLOWED: dict[tuple[Domain, Domain], frozenset[type[BoundaryEvent]]] = {
    (Domain.CONTENT, Domain.TRADING): frozenset({TradingIdeaProposed}),
    (Domain.CONTENT, Domain.PRIVATE): frozenset({LifeTipProposed}),
    (Domain.TRADING, Domain.CORE): frozenset({TaskMetadata, AuditAnchor}),
    (Domain.CONTENT, Domain.CORE): frozenset({TaskMetadata, AuditAnchor, ContextReviewRequested}),
    (Domain.PRIVATE, Domain.CORE): frozenset({TaskMetadata, AuditAnchor}),
    (Domain.CORE, Domain.CONTENT): frozenset({ReelSubmitted, ContextReviewDecided}),
}


@dataclass(frozen=True)
class FlowDecision:
    allowed: bool
    reason: str


def check_flow(event: BoundaryEvent, *, caller_domain: Domain) -> FlowDecision:
    """Decide a boundary crossing. The origin comes from the caller's identity, not the payload."""
    if event.origin_domain is not caller_domain:
        return FlowDecision(False, "origin_domain does not match caller identity")
    if event.classification is DataClassification.SECRET:
        return FlowDecision(False, "SECRET never crosses a boundary")
    if event.origin_domain is event.target_domain:
        return FlowDecision(True, "same domain")
    allowed_types = _ALLOWED.get((event.origin_domain, event.target_domain), frozenset())
    if type(event) not in allowed_types:
        return FlowDecision(
            False,
            f"{type(event).__name__} not allowed from {event.origin_domain} "
            f"to {event.target_domain}",
        )
    if isinstance(event, ContextReviewRequested) and event.classification.at_least(
        DataClassification.CONFIDENTIAL
    ):
        return FlowDecision(False, "only PUBLIC or INTERNAL material is shown for review")
    if event.origin_domain is Domain.PRIVATE and not isinstance(event, TaskMetadata | AuditAnchor):
        return FlowDecision(False, "only metadata leaves the private domain")
    return FlowDecision(True, "allowed by flow matrix")
