"""Workstation entry point for the content domain's context curation.

    uv run python -m mod_content.curation --data-dir ~/jarvis-data/content run
    uv run python -m mod_content.curation --data-dir ~/jarvis-data/content run --watch
    uv run python -m mod_content.curation --data-dir ~/jarvis-data/content review
    uv run python -m mod_content.curation --data-dir ~/jarvis-data/content decide <id> accept
    uv run python -m mod_content.curation --data-dir ~/jarvis-data/content context
    uv run python -m mod_content.curation --data-dir ~/jarvis-data/content report

The data directory is the reel pipeline's: runs.jsonl is read, curation/ is written. Curation
keeps its own audit chain (curation/audit.jsonl) so it can run next to `reel serve`, which
holds the head of the reel audit chain in memory.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from jarvis_core.audit.chain import AuditChainError, AuditWriter, verify_chain
from jarvis_core.curation.config import load_config
from jarvis_core.curation.models import Outcome
from jarvis_core.curation.pipeline import CurationPipeline, ReviewNotFound
from jarvis_core.curation.store import FileCurationStore, open_reviews
from mod_content.curation.sources import reel_items
from mod_content.reel.store import FileAuditStore

DEFAULT_DATA = Path(os.environ.get("JARVIS_CONTENT_DATA", "/var/lib/jarvis/content"))
DEFAULT_CATEGORIES = Path(__file__).with_name("categories.yaml")
ACTOR = "spiffe://jarvis.home/content/curation"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mod_content.curation")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--categories", type=Path, default=DEFAULT_CATEGORIES)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="collect, consolidate, prepare and decide")
    run.add_argument("--watch", action="store_true", help="keep running as new reels arrive")
    run.add_argument("--poll-seconds", type=float, default=30.0)

    sub.add_parser("review", help="list decisions waiting for you")
    decide = sub.add_parser("decide", help="accept or reject an open review")
    decide.add_argument("brief_id", help="id from `review`; a unique prefix is enough")
    decide.add_argument("verdict", choices=("accept", "reject"))
    decide.add_argument("--note", default="")

    context = sub.add_parser("context", help="show curated context")
    context.add_argument("--category")
    sub.add_parser("report", help="counts per stage and category, audit chain state")

    args = parser.parse_args(argv)
    data: Path = args.data_dir
    root = data / "curation"
    store = FileCurationStore(root)
    if args.command == "review":
        return _review(store)
    if args.command == "context":
        return _context(store, args.category)
    if args.command == "report":
        return _report(store, root)

    pipeline = CurationPipeline(
        config=load_config(args.categories),
        store=store,
        audit=AuditWriter(FileAuditStore(root / "audit.jsonl")).write,
        actor=ACTOR,
    )
    if args.command == "decide":
        return _decide(pipeline, store, args.brief_id, args.verdict == "accept", args.note)
    while True:
        counts = pipeline.run(reel_items(data / "runs.jsonl"))
        if counts.collected or counts.rejected or not args.watch:
            print(counts.model_dump_json(), flush=True)
        if not args.watch:
            return 0
        time.sleep(args.poll_seconds)


def _review(store: FileCurationStore) -> int:
    briefs = open_reviews(store.decisions())
    if not briefs:
        print("nothing waiting for review")
    for b in briefs:
        d = b.digest
        print(f"{b.brief_id}  [{b.category}] {b.proposal.value} conf={b.confidence:.2f}")
        print(f"  {d.title}")
        print(f"  why: {'; '.join(b.reasons)}")
        if d.flags:
            print(f"  FLAGS: {', '.join(d.flags)}")
        for fact in d.facts[:5]:
            print(f"   - {fact}")
        print(f"  sources: {', '.join(d.source_refs)}")
    return 0


def _decide(
    pipeline: CurationPipeline, store: FileCurationStore, prefix: str, accept: bool, note: str
) -> int:
    matches = [b.brief_id for b in open_reviews(store.decisions()) if b.brief_id.startswith(prefix)]
    if len(matches) != 1:
        print(f"{len(matches)} open reviews match {prefix!r}", file=sys.stderr)
        return 2
    try:
        record = pipeline.review(matches[0], accept, by="user:owner", note=note)
    except ReviewNotFound:
        print(f"no open review {matches[0]}", file=sys.stderr)
        return 2
    print(f"{record.brief.brief_id}: {record.outcome.value}")
    return 0


def _context(store: FileCurationStore, category: str | None) -> int:
    for e in store.entries(category):
        print(f"[{e.category}] {e.title}  (v{e.version}, {len(e.source_refs)} source(s))")
        for fact in e.facts:
            print(f"   - {fact}")
    return 0


def _report(store: FileCurationStore, root: Path) -> int:
    decisions = store.decisions()
    latest = {d.brief.brief_id: d for d in decisions}
    outcomes = {o.value: 0 for o in Outcome}
    for d in latest.values():
        outcomes[d.outcome.value] += 1
    per_category: dict[str, int] = {}
    for e in store.entries():
        per_category[e.category] = per_category.get(e.category, 0) + 1
    intact: bool | None = None
    if (root / "audit.jsonl").exists():
        try:
            intact = verify_chain(FileAuditStore(root / "audit.jsonl").entries()) is None
        except AuditChainError:
            intact = False
    items = store.items()
    report = {
        "collected": len(items),
        "duplicates_merged": sum(d.brief.digest.duplicates_dropped for d in latest.values()),
        "briefs": len(latest),
        "outcomes": outcomes,
        "open_reviews": len(open_reviews(decisions)),
        "context_entries": per_category,
        "audit_chain_intact": intact,
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
