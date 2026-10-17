"""Workstation entry point for the trading domain's context curation.

    uv run python -m mod_trading.curation --data-dir ~/jarvis-data/trading run
    uv run python -m mod_trading.curation --data-dir ~/jarvis-data/trading review
    uv run python -m mod_trading.curation --data-dir ~/jarvis-data/trading decide <id> accept
    uv run python -m mod_trading.curation --data-dir ~/jarvis-data/trading search qqq

Reads the trading inbox (ideas.json) and daily bars in <data-dir>/daily/*.csv (written by
`python -m mod_trading history --instrument QQQ --out <data-dir>/daily/QQQ.csv`) and writes
only to <data-dir>/curation/. Reviews stay in the terminal: trading context is PRIVATE and never
shown outside the trading domain.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from jarvis_core.curation.cli import DomainCli, run_cli
from jarvis_core.curation.models import CollectedItem
from mod_trading.curation.regime import regime_items
from mod_trading.curation.sources import idea_items

DEFAULT_DATA = Path(os.environ.get("JARVIS_TRADING_DATA", "/var/lib/jarvis/trading"))
DEFAULT_CATEGORIES = Path(__file__).with_name("categories.yaml")


def _sources(data: Path, _root: Path) -> Iterator[CollectedItem]:
    yield from idea_items(data / "ideas.json")
    yield from regime_items(data / "daily")


CLI = DomainCli(
    prog="python -m mod_trading.curation",
    domain="trading",
    actor="spiffe://jarvis.home/trading/curation",
    default_data=DEFAULT_DATA,
    default_categories=DEFAULT_CATEGORIES,
    sources=_sources,
    watched=lambda data, _root: (data / "ideas.json", data / "daily"),
)


def main(argv: list[str] | None = None) -> int:
    return run_cli(CLI, argv)


if __name__ == "__main__":
    raise SystemExit(main())
