"""Workstation entry point for the content domain's context curation.

    uv run python -m mod_content.curation --data-dir ~/jarvis-data/content run
    uv run python -m mod_content.curation --data-dir ~/jarvis-data/content run --watch \\
        --boundary-dir ~/.local/share/jarvis-deck-local/boundary
    uv run python -m mod_content.curation --data-dir ~/jarvis-data/content review
    uv run python -m mod_content.curation --data-dir ~/jarvis-data/content decide <id> accept
    uv run python -m mod_content.curation --data-dir ~/jarvis-data/content context
    uv run python -m mod_content.curation --data-dir ~/jarvis-data/content report
    uv run python -m mod_content.curation --data-dir ~/jarvis-data/content search "qqq breakout"
    uv run python -m mod_content.curation --data-dir ~/jarvis-data/content export

The data directory is the reel pipeline's: runs.jsonl is read, curation/ is written. Notes
dropped into <data-dir>/curation/inbox/ (.md or .txt) are collected like reels. Curation keeps
its own audit chain (curation/audit.jsonl) so it can run next to `reel serve`, which holds the
head of the reel audit chain in memory.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

from jarvis_core.curation.cli import Bridge, DomainCli, inputs_signature, run_cli
from jarvis_core.curation.models import CollectedItem
from jarvis_core.curation.pipeline import CurationPipeline
from jarvis_core.curation.store import FileCurationStore
from mod_content.curation.deck import DeckReviews, review_mailbox
from mod_content.curation.notes import note_items
from mod_content.curation.sources import reel_items

DEFAULT_DATA = Path(os.environ.get("JARVIS_CONTENT_DATA", "/var/lib/jarvis/content"))
DEFAULT_CATEGORIES = Path(__file__).with_name("categories.yaml")

__all__ = ["DEFAULT_CATEGORIES", "inputs_signature", "main"]


def _sources(data: Path, root: Path) -> Iterator[CollectedItem]:
    yield from reel_items(data / "runs.jsonl")
    yield from note_items(root / "inbox")


def _run_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--boundary-dir",
        type=Path,
        help="send open reviews to the deck and apply its answers (same as the gateway's)",
    )


def _deck(
    args: argparse.Namespace, pipeline: CurationPipeline, store: FileCurationStore, root: Path
) -> Bridge | None:
    if args.boundary_dir is None:
        return None
    deck = DeckReviews(pipeline, store, review_mailbox(args.boundary_dir), root / "deck-sent.json")
    return lambda: asdict(deck.sync())


CLI = DomainCli(
    prog="python -m mod_content.curation",
    domain="content",
    actor="spiffe://jarvis.home/content/curation",
    default_data=DEFAULT_DATA,
    default_categories=DEFAULT_CATEGORIES,
    sources=_sources,
    watched=lambda data, root: (data / "runs.jsonl", root / "inbox"),
    add_run_options=_run_options,
    make_bridge=_deck,
)


def main(argv: list[str] | None = None) -> int:
    return run_cli(CLI, argv)


if __name__ == "__main__":
    raise SystemExit(main())
