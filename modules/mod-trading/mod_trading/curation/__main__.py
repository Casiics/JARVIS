"""Workstation entry point for the trading domain's context curation.

    uv run python -m mod_trading.curation --data-dir ~/jarvis-data/trading run
    uv run python -m mod_trading.curation --data-dir ~/jarvis-data/trading review
    uv run python -m mod_trading.curation --data-dir ~/jarvis-data/trading decide <id> accept
    uv run python -m mod_trading.curation --data-dir ~/jarvis-data/trading search qqq

Reads the trading inbox (ideas.json) and writes only to <data-dir>/curation/. Reviews stay in
the terminal: trading context is PRIVATE and never shown outside the trading domain.
"""

from __future__ import annotations

import os
from pathlib import Path

from jarvis_core.curation.cli import DomainCli, run_cli
from mod_trading.curation.sources import idea_items

DEFAULT_DATA = Path(os.environ.get("JARVIS_TRADING_DATA", "/var/lib/jarvis/trading"))
DEFAULT_CATEGORIES = Path(__file__).with_name("categories.yaml")

CLI = DomainCli(
    prog="python -m mod_trading.curation",
    domain="trading",
    actor="spiffe://jarvis.home/trading/curation",
    default_data=DEFAULT_DATA,
    default_categories=DEFAULT_CATEGORIES,
    sources=lambda data, _root: idea_items(data / "ideas.json"),
    watched=lambda data, _root: (data / "ideas.json",),
)


def main(argv: list[str] | None = None) -> int:
    return run_cli(CLI, argv)


if __name__ == "__main__":
    raise SystemExit(main())
