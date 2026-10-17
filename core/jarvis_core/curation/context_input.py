"""Curated context as a named input of an orchestrator work order.

A domain selects entries from its own curation store, renders them as a pack and attaches the
pack to a `WorkOrder`. The checks here keep the order honest about what it carries:

- every entry must come from the order's domain (no cross-domain context, CLAUDE.md 2.1);
- the order's label must be at least the strictest entry label (labels only rise, 2.2);
- context derived from external content needs an order whose task token is tainted, so curated
  reel material cannot lift an untainted task into a higher approval tier (2.5).

Entries that would break the label ceiling are dropped before rendering instead of failing the
whole run, because the ceiling is the caller's deliberate choice of what the task may see.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from jarvis_core.contracts.classification import DataClassification, Domain, classify
from jarvis_core.contracts.events import Taint
from jarvis_core.curation.models import ContextEntry
from jarvis_core.curation.retrieval import build_pack, search
from jarvis_core.orchestrator.loop import WorkOrder

INPUT_NAME = "curated_context"


class ContextInputError(ValueError):
    """The context does not fit the work order it was meant for."""


@dataclass(frozen=True)
class ContextInput:
    domain: Domain
    label: DataClassification
    taint: Taint
    entry_ids: tuple[str, ...]
    text: str
    name: str = INPUT_NAME

    @property
    def empty(self) -> bool:
        return not self.entry_ids


def select_context(
    entries: Sequence[ContextEntry],
    *,
    domain: Domain,
    ceiling: DataClassification,
    query: str | None = None,
    category: str | None = None,
    k: int = 8,
    max_chars: int = 6000,
) -> ContextInput:
    """Pick up to `k` entries (by query when given, else by strength) and render them as a pack."""
    foreign = sorted({e.domain.value for e in entries if e.domain is not domain})
    if foreign:
        raise ContextInputError(f"entries from other domains: {', '.join(foreign)}")
    allowed = [
        e
        for e in entries
        if ceiling.at_least(classify(e.domain, e.classification))
        and (category is None or e.category == category)
    ]
    if query:
        chosen = [hit.entry for hit in search(allowed, query, k=k)]
    else:
        chosen = sorted(allowed, key=lambda e: (-e.signal, -len(e.source_refs), e.entry_id))[:k]
    text, included = build_pack(chosen, domain=domain.value, max_chars=max_chars)
    label = max(
        (classify(e.domain, e.classification) for e in included),
        key=lambda c: c.rank,
        default=classify(domain, DataClassification.PUBLIC),
    )
    tainted = any(e.taint is not Taint.NONE for e in included)
    return ContextInput(
        domain=domain,
        label=label,
        taint=Taint.EXTERNAL_CONTENT if tainted else Taint.NONE,
        entry_ids=tuple(e.entry_id for e in included),
        text=text,
    )


def with_context(order: WorkOrder, context: ContextInput) -> WorkOrder:
    """The order with the context pack as its first input. Empty context leaves it unchanged."""
    if context.empty:
        return order
    if context.domain is not order.claims.domain:
        raise ContextInputError("context domain differs from the task token's domain")
    if not order.label.at_least(context.label):
        raise ContextInputError("context label exceeds the work order's label")
    if context.taint is not Taint.NONE and order.claims.taint is Taint.NONE:
        raise ContextInputError("external-derived context needs a tainted task token")
    if any(name == context.name for name, _ in order.inputs):
        raise ContextInputError(f"work order already has an input named {context.name!r}")
    return replace(order, inputs=((context.name, context.text), *order.inputs))
