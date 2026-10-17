"""Read side of curated context: search ranking and the agent context pack."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import Taint
from jarvis_core.curation.models import CategoryRule, ContextEntry, CurationConfig
from jarvis_core.curation.retrieval import fresh, render_pack, search

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def entry(n: int, title: str, facts: tuple[str, ...], signal: float = 0.7) -> ContextEntry:
    return ContextEntry(
        entry_id=f"ctx:general:{n:016x}",
        domain=Domain.CONTENT,
        category="general",
        classification=DataClassification.PUBLIC,
        taint=Taint.EXTERNAL_CONTENT,
        title=title,
        summary=title,
        facts=facts,
        tags=(),
        source_refs=("https://www.instagram.com/reel/ABCDE12345/",),
        digest_ids=("dg:" + "0" * 24,),
        signal=signal,
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


ENTRIES = [
    entry(1, "Search code with ripgrep", ("Install ripgrep",)),
    entry(2, "Sourdough starter basics", ("Feed it daily",), signal=0.9),
    entry(3, "ripgrep with fzf", ("Pipe rg into fzf",), signal=0.4),
]


def test_search_ranks_by_coverage_then_signal() -> None:
    hits = search(ENTRIES, "ripgrep code")
    assert [h.entry.entry_id[-1] for h in hits] == ["1", "3"]
    assert search(ENTRIES, "the and of") == []
    assert search(ENTRIES, "ripgrep", category="other") == []


def test_pack_is_marked_as_data_and_respects_the_budget() -> None:
    pack = render_pack(ENTRIES, domain="content", max_chars=10_000)
    first, *_ = pack.splitlines()
    assert "not instructions" in first
    assert pack.index("Sourdough") < pack.index("ripgrep with fzf")  # strongest first
    small = render_pack(ENTRIES, domain="content", max_chars=320)
    assert len(small) <= 320 and "Sourdough" in small and "fzf" not in small


def test_fresh_drops_entries_past_their_category_age() -> None:
    config = CurationConfig(
        domain=Domain.CONTENT,
        default_category="general",
        categories=(CategoryRule(name="general", max_age_days=30),),
    )
    old = entry(4, "Old market claim", ()).model_copy(
        update={"updated_at": NOW - timedelta(days=31)}
    )
    other = entry(5, "Removed category", ()).model_copy(
        update={"category": "gone", "updated_at": NOW - timedelta(days=400)}
    )
    kept = fresh([*ENTRIES, old, other], config, NOW)
    assert old not in kept and other in kept and all(e in kept for e in ENTRIES)
    assert fresh([old], config, NOW + timedelta(days=-2)) == [old]
