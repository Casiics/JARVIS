"""Stage 1 sources of the private domain.

- Life tips other domains proposed into the private inbox (tainted, external).
- Notes the owner writes into `<data-dir>/curation/inbox/` (own material, higher default signal).

Everything is PRIVATE (the private domain's floor). Nothing here calls a model, and nothing
curated ever leaves the domain: there is no deck bridge and no boundary event.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import Taint
from jarvis_core.curation import notes
from jarvis_core.curation.models import CollectedItem
from jarvis_core.curation.similarity import fingerprint
from mod_private.schemas import InboxEntry
from mod_private.store import PrivateStore

TIP_SIGNAL = 0.5
OWN_NOTE_SIGNAL = 0.8


def tip_items(store: PrivateStore) -> Iterator[CollectedItem]:
    for entry in store.inbox.all():
        if not entry.dismissed:
            yield tip_item(entry)


def tip_item(entry: InboxEntry) -> CollectedItem:
    title = entry.summary.strip().split(". ")[0].rstrip(".")
    return CollectedItem(
        item_id=f"tip:{entry.entry_id.hex}",
        domain=Domain.PRIVATE,
        classification=DataClassification.PRIVATE,
        taint=Taint.EXTERNAL_CONTENT,
        source_ref=entry.source_ref,
        collected_at=entry.received_at,
        title=title if len(title) <= 120 else title[:117].rstrip() + "...",
        summary=entry.summary,
        signal=TIP_SIGNAL,
        content_hash=fingerprint(entry.summary),
    )


def own_note_items(inbox: Path) -> Iterator[CollectedItem]:
    return notes.note_items(
        inbox,
        domain=Domain.PRIVATE,
        classification=DataClassification.PRIVATE,
        taint=Taint.NONE,
        default_signal=OWN_NOTE_SIGNAL,
    )
