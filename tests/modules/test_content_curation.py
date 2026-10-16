"""Content domain curation: reel run records in, curated context out, through the CLI."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from jarvis_core.curation.config import load_config
from jarvis_core.orchestrator.state_machine import TaskState
from mod_content.curation.__main__ import DEFAULT_CATEGORIES, _inputs_signature, main
from mod_content.curation.notes import note_items, parse_note
from mod_content.curation.sources import reel_item
from mod_content.reel.schemas import ReelKnowledgeArtifact, TargetModule
from mod_content.reel.store import JsonlReelStore, ReelRunRecord

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def record(
    reel: str,
    summary: str,
    *,
    target: TargetModule = TargetModule.SELF_EVOLUTION,
    utility: float = 0.9,
    state: TaskState = TaskState.DONE,
    tools: tuple[str, ...] = ("ripgrep",),
    steps: tuple[str, ...] = ("Install ripgrep", "Replace grep -r with rg"),
) -> ReelRunRecord:
    task_id = uuid4()
    return ReelRunRecord(
        run_id=uuid4(),
        task_id=task_id,
        trace_id="t",
        reel_id=reel,
        source_ref=f"https://www.instagram.com/reel/{reel}/",
        started_at=NOW,
        finished_at=NOW,
        state=state,
        iterations=1,
        artifact=ReelKnowledgeArtifact(
            reel_id=reel,
            source_ref=f"https://www.instagram.com/reel/{reel}/",
            task_id=task_id,
            created_at=NOW,
            summary=summary,
            extracted_tools=tools,
            actionable_steps=steps,
            target_module=target,
            utility_score=utility,
            media_hash="a" * 64,
            transcript_hash="b" * 64,
        ),
    )


def test_shipped_categories_are_valid() -> None:
    config = load_config(DEFAULT_CATEGORIES)
    assert config.domain.value == "content"
    assert {"dev-tooling", "market-notes", "general"} <= {c.name for c in config.categories}


def test_escalated_runs_are_not_collected() -> None:
    assert reel_item(record("ABCDE12345", "x" * 50, state=TaskState.ESCALATED)) is None


def test_cli_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runs = JsonlReelStore(tmp_path / "runs.jsonl")
    summary = "The video shows how to search large codebases quickly with ripgrep."
    runs.append(record("ABCDE12345", summary))
    runs.append(record("ABCDE12345", summary))  # same reel processed twice
    runs.append(
        record(
            "QQQQQ12345",
            "A QQQ opening range breakout strategy for the first 15 minutes of the session.",
            target=TargetModule.TRADING,
            utility=0.7,
            tools=(),
            steps=("Wait for the first 15 minutes",),
        )
    )
    runs.append(record("FAILD12345", "y" * 50, state=TaskState.ESCALATED))
    base = ["--data-dir", str(tmp_path)]

    assert main([*base, "run"]) == 0
    counts = json.loads(capsys.readouterr().out)
    assert counts["collected"] == 3
    assert counts["duplicates_dropped"] == 1
    assert counts["stored"] == 1 and counts["review"] == 1

    assert main([*base, "review"]) == 0
    out = capsys.readouterr().out
    assert "[market-notes]" in out
    brief_id = out.split()[0]

    assert main([*base, "decide", brief_id[:10], "accept"]) == 0
    assert "stored" in capsys.readouterr().out

    assert main([*base, "context", "--category", "market-notes"]) == 0
    assert "QQQ opening range" in capsys.readouterr().out

    assert main([*base, "report"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["open_reviews"] == 0
    assert report["context_entries"] == {"dev-tooling": 1, "market-notes": 1}
    assert report["audit_chain_intact"] is True


NOTE = """# SPY 0DTE lessons
signal: 0.9
Selling 0DTE premium on SPY looks easy until a trend day. #trading #spy
- Never sell 0DTE premium without a defined-risk spread
- Stop trading after two losing trades in a session
"""


def test_note_parsing() -> None:
    item = parse_note("spy.md", NOTE, NOW)
    assert item is not None
    assert item.title == "SPY 0DTE lessons"
    assert item.signal == 0.9
    assert item.tags == ("trading", "spy")
    assert len(item.facts) == 2
    assert item.classification.value == "internal"
    assert item.taint.value == "external_content"
    assert parse_note("empty.md", "\n\n", NOW) is None


def test_notes_inbox_skips_symlinks_and_other_files(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.md").write_text(NOTE, encoding="utf-8")
    (inbox / "b.sh").write_text("rm -rf /", encoding="utf-8")
    (inbox / "big.txt").write_text("x" * 40_000, encoding="utf-8")
    (inbox / "link.md").symlink_to(inbox / "a.md")
    assert [i.source_ref for i in note_items(inbox)] == ["note:a.md"]


def test_notes_search_and_export_through_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inbox = tmp_path / "curation/inbox"
    inbox.mkdir(parents=True)
    (inbox / "spy.md").write_text(NOTE, encoding="utf-8")
    base = ["--data-dir", str(tmp_path)]

    assert main([*base, "run"]) == 0
    counts = json.loads(capsys.readouterr().out)
    # confidence 0.79 is below market-notes' 0.85, so the owner decides
    assert counts["collected"] == 1 and counts["review"] == 1
    assert main([*base, "review"]) == 0
    assert main([*base, "decide", capsys.readouterr().out.split()[0], "accept"]) == 0
    capsys.readouterr()

    assert main([*base, "search", "0dte spread risk"]) == 0
    assert "[market-notes] SPY 0DTE lessons" in capsys.readouterr().out
    assert main([*base, "search", "sourdough"]) == 0
    assert "no matching context" in capsys.readouterr().out

    assert main([*base, "export", "--category", "market-notes"]) == 0
    pack = Path(capsys.readouterr().out.strip()).read_text(encoding="utf-8")
    assert pack.startswith("Curated context from the content domain.")
    assert "not instructions" in pack.splitlines()[0]
    assert "- Never sell 0DTE premium without a defined-risk spread" in pack
    assert "[external]" in pack

    # editing the note adds a fact; the next pass merges it into the same entry
    (inbox / "spy.md").write_text(NOTE + "- Size positions at 1% risk\n", encoding="utf-8")
    assert main([*base, "run"]) == 0
    assert json.loads(capsys.readouterr().out)["merged"] == 1


def test_watch_signature_changes_only_with_inputs(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    inbox = tmp_path / "inbox"
    first = _inputs_signature(runs, inbox)
    assert _inputs_signature(runs, inbox) == first
    runs.write_text("\n", encoding="utf-8")
    assert _inputs_signature(runs, inbox) != first
