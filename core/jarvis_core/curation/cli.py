"""Shared command line for a domain's context curation.

Each domain supplies its sources, its categories file and, optionally, a bridge that runs after
every pass (content uses it for deck reviews). The commands and file layout are the same in
every domain: `<data-dir>/curation/` holds items, decisions, context, packs and the audit chain.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from jarvis_core.audit.chain import AuditChainError, AuditWriter, verify_chain
from jarvis_core.audit.file import FileAuditStore
from jarvis_core.curation.config import load_config
from jarvis_core.curation.models import CollectedItem, Outcome
from jarvis_core.curation.pipeline import CurationPipeline, ReviewNotFound
from jarvis_core.curation.retrieval import render_pack, search
from jarvis_core.curation.store import FileCurationStore, open_reviews

Bridge = Callable[[], dict[str, int]]


@dataclass(frozen=True)
class DomainCli:
    prog: str
    domain: str
    actor: str
    default_data: Path
    default_categories: Path
    sources: Callable[[Path, Path], Iterable[CollectedItem]]
    """(data_dir, curation_root) -> items for stage 1."""
    watched: Callable[[Path, Path], tuple[Path, ...]]
    """Files or directories whose change triggers a new pass in --watch mode."""
    add_run_options: Callable[[argparse.ArgumentParser], None] | None = None
    make_bridge: (
        Callable[[argparse.Namespace, CurationPipeline, FileCurationStore, Path], Bridge | None]
        | None
    ) = None


def run_cli(cli: DomainCli, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=cli.prog)
    parser.add_argument("--data-dir", type=Path, default=cli.default_data)
    parser.add_argument("--categories", type=Path, default=cli.default_categories)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="collect, consolidate, prepare and decide")
    run.add_argument("--watch", action="store_true", help="keep running as new material arrives")
    run.add_argument("--poll-seconds", type=float, default=30.0)
    if cli.add_run_options is not None:
        cli.add_run_options(run)

    sub.add_parser("review", help="list decisions waiting for you")
    decide = sub.add_parser("decide", help="accept or reject an open review")
    decide.add_argument("brief_id", help="id from `review`; a unique prefix is enough")
    decide.add_argument("verdict", choices=("accept", "reject"))
    decide.add_argument("--note", default="")

    context = sub.add_parser("context", help="show curated context")
    context.add_argument("--category")
    sub.add_parser("report", help="counts per stage and category, audit chain state")
    find = sub.add_parser("search", help="find curated entries for a question")
    find.add_argument("query")
    find.add_argument("--category")
    find.add_argument("-k", type=int, default=5)
    export = sub.add_parser("export", help="write a context pack for agents (Markdown)")
    export.add_argument("--category")
    export.add_argument("--max-chars", type=int, default=8000)

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
    if args.command == "search":
        return _search(store, args.query, args.category, args.k)
    if args.command == "export":
        return _export(store, root, cli.domain, args.category, args.max_chars)

    root.mkdir(parents=True, exist_ok=True)
    pipeline = CurationPipeline(
        config=load_config(args.categories),
        store=store,
        audit=AuditWriter(FileAuditStore(root / "audit.jsonl")).write,
        actor=cli.actor,
    )
    if args.command == "decide":
        return _decide(pipeline, store, args.brief_id, args.verdict == "accept", args.note)
    bridge = cli.make_bridge(args, pipeline, store, root) if cli.make_bridge else None
    last_seen: tuple[tuple[str, int, int], ...] | None = None
    while True:
        seen = inputs_signature(*cli.watched(data, root))
        if seen != last_seen:  # a full pass only when an input changed
            counts = pipeline.run(cli.sources(data, root))
            last_seen = seen
            if counts.collected or counts.rejected or not args.watch:
                print(counts.model_dump_json(), flush=True)
        if bridge is not None:
            synced = bridge()
            if any(synced.values()):
                print(json.dumps({"bridge": synced}), flush=True)
        if not args.watch:
            return 0
        time.sleep(args.poll_seconds)


def inputs_signature(*paths: Path) -> tuple[tuple[str, int, int], ...]:
    files: list[Path] = []
    for path in paths:
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(p for p in path.iterdir() if p.is_file())
    out = []
    for file in sorted(files):
        st = file.stat()
        out.append((str(file), st.st_mtime_ns, st.st_size))
    return tuple(out)


def _search(store: FileCurationStore, query: str, category: str | None, k: int) -> int:
    hits = search(store.entries(category), query, k=k)
    if not hits:
        print("no matching context")
    for hit in hits:
        e = hit.entry
        print(f"{hit.score:.2f}  [{e.category}] {e.title}")
        for fact in e.facts[:5]:
            print(f"   - {fact}")
    return 0


def _export(
    store: FileCurationStore, root: Path, domain: str, category: str | None, max_chars: int
) -> int:
    pack = render_pack(store.entries(category), domain=domain, max_chars=max_chars)
    out = root / "packs" / f"{category or 'all'}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(pack, encoding="utf-8")
    print(out)
    return 0


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
