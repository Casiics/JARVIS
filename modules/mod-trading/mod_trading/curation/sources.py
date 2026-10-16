"""Stage 1 source of the trading domain: ideas in the trading inbox.

Ideas arrive from content as tainted proposals. Curation turns them into research context for
trading's own agents (market researcher, model builder); it never creates a signal or an order.
The inbox stores no classification, so items fail closed to PRIVATE (CLAUDE.md, rule 2).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.curation.models import CollectedItem
from jarvis_core.curation.similarity import fingerprint
from mod_trading.inbox import Idea, TradingInbox

BASE_SIGNAL = 0.5
# The owner's focus instruments: an idea naming one of them is more useful to research.
WATCHLIST = frozenset({"QQQ", "SPY", "IWM"})
WATCHLIST_BONUS = 0.2


def idea_items(inbox_file: Path, watchlist: frozenset[str] = WATCHLIST) -> Iterator[CollectedItem]:
    for idea in TradingInbox(inbox_file).all():
        if not idea.dismissed:
            yield idea_item(idea, watchlist)


def idea_item(idea: Idea, watchlist: frozenset[str] = WATCHLIST) -> CollectedItem:
    instruments = tuple(i.upper() for i in idea.instruments)
    mentioned = {w for w in watchlist if w in instruments or w in idea.summary.upper().split()}
    signal = BASE_SIGNAL + (WATCHLIST_BONUS if mentioned else 0.0)
    title = idea.summary.strip().split(". ")[0].rstrip(".")
    return CollectedItem(
        item_id=f"idea:{idea.idea_id.hex}",
        domain=Domain.TRADING,
        classification=DataClassification.PRIVATE,
        taint=idea.taint,
        source_ref=idea.source_ref,
        collected_at=idea.received_at,
        title=title if len(title) <= 120 else title[:117].rstrip() + "...",
        summary=idea.summary[:4000],
        facts=(),
        tags=tuple(i.casefold() for i in instruments)
        + tuple(sorted(m.casefold() for m in mentioned)),
        signal=signal,
        content_hash=fingerprint(idea.summary),
    )
