"""Context curation for the private domain. Local only; nothing leaves the domain.

    uv run python -m mod_private.curation --data-dir ~/jarvis-data/private run
    uv run python -m mod_private.curation --data-dir ~/jarvis-data/private review
    uv run python -m mod_private.curation --data-dir ~/jarvis-data/private decide <id> accept
    uv run python -m mod_private.curation --data-dir ~/jarvis-data/private search "sleep"

Reads the private inbox (inbox.json, life tips from other domains) and your own notes in
<data-dir>/curation/inbox/, and writes only to <data-dir>/curation/.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from jarvis_core.curation.cli import DomainCli, run_cli
from jarvis_core.curation.models import CollectedItem
from mod_private.curation.sources import own_note_items, tip_items
from mod_private.store import PrivateStore

DEFAULT_DATA = Path(os.environ.get("JARVIS_PRIVATE_DATA", "/var/lib/jarvis/private"))
DEFAULT_CATEGORIES = Path(__file__).with_name("categories.yaml")


def _sources(data: Path, root: Path) -> Iterator[CollectedItem]:
    yield from tip_items(PrivateStore(data))
    yield from own_note_items(root / "inbox")


CLI = DomainCli(
    prog="python -m mod_private.curation",
    domain="private",
    actor="spiffe://jarvis.home/private/curation",
    default_data=DEFAULT_DATA,
    default_categories=DEFAULT_CATEGORIES,
    sources=_sources,
    watched=lambda data, root: (data / "inbox.json", root / "inbox"),
)


def main(argv: list[str] | None = None) -> int:
    return run_cli(CLI, argv)


if __name__ == "__main__":
    raise SystemExit(main())
