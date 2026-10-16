"""Stage 1 source for notes the owner drops into `<data-dir>/curation/inbox/`.

One Markdown or text file per note. The first non-empty line is the title, bullet lines
(`- ` or `* `) become facts, `#hashtags` become tags, the rest is the summary. An optional
`signal: 0.8` line sets the usefulness estimate (default 0.6). Files are read, never executed or
followed; anything unreadable or too large is skipped.

Notes are treated as external content like reels: they are often pasted from somewhere else.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import Taint
from jarvis_core.curation.models import CollectedItem
from jarvis_core.curation.similarity import fingerprint, unique

MAX_NOTE_BYTES = 32 * 1024
DEFAULT_SIGNAL = 0.6
SUFFIXES = frozenset({".md", ".txt"})
_HASHTAG = re.compile(r"(?<![\w#])#([a-z0-9][a-z0-9-]{1,39})", re.IGNORECASE)
_SIGNAL = re.compile(r"^signal:\s*([01](?:\.\d+)?)\s*$", re.IGNORECASE)


def note_items(inbox: Path) -> Iterator[CollectedItem]:
    if not inbox.is_dir():
        return
    for path in sorted(inbox.iterdir()):
        if path.suffix.lower() not in SUFFIXES or path.is_symlink() or not path.is_file():
            continue
        if path.stat().st_size > MAX_NOTE_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        item = parse_note(path.name, text, datetime.fromtimestamp(path.stat().st_mtime, UTC))
        if item is not None:
            yield item


def parse_note(name: str, text: str, mtime: datetime) -> CollectedItem | None:
    title = ""
    signal = DEFAULT_SIGNAL
    facts: list[str] = []
    body: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if match := _SIGNAL.match(line):
            signal = min(1.0, float(match.group(1)))
            continue
        if not title:
            title = line.lstrip("# ").strip()[:200]
            continue
        if line.startswith(("- ", "* ")):
            facts.append(line[2:].strip()[:300])
        else:
            body.append(line)
    summary = " ".join(body)[:4000] or title
    if not title:
        return None
    tags = unique([t.casefold() for t in _HASHTAG.findall(text)], 20)
    facts_t = unique(facts, 40)
    content_hash = fingerprint(summary, *facts_t)
    ident = hashlib.sha256(f"{name}\x1f{content_hash}".encode()).hexdigest()[:24]
    try:
        return CollectedItem(
            item_id=f"note:{ident}",
            domain=Domain.CONTENT,
            classification=DataClassification.INTERNAL,
            taint=Taint.EXTERNAL_CONTENT,
            source_ref=f"note:{name[:200]}",
            collected_at=mtime,
            title=title,
            summary=summary,
            facts=facts_t,
            tags=tags,
            signal=signal,
            content_hash=content_hash,
        )
    except ValidationError:
        return None
