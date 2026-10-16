"""Notes the owner drops into the content domain's `curation/inbox/`.

Treated as external content like reels, because they are often pasted from somewhere else, and
labelled INTERNAL so they may be reviewed in the deck.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import Taint
from jarvis_core.curation import notes
from jarvis_core.curation.models import CollectedItem


def note_items(inbox: Path) -> Iterator[CollectedItem]:
    return notes.note_items(
        inbox,
        domain=Domain.CONTENT,
        classification=DataClassification.INTERNAL,
        taint=Taint.EXTERNAL_CONTENT,
    )


def parse_note(name: str, text: str, mtime: datetime) -> CollectedItem | None:
    return notes.parse_note(
        name,
        text,
        mtime,
        domain=Domain.CONTENT,
        classification=DataClassification.INTERNAL,
        taint=Taint.EXTERNAL_CONTENT,
    )
