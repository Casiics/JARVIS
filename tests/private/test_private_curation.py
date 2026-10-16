"""Private curation: life tips and own notes become PRIVATE context that never leaves the domain."""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import Taint
from jarvis_core.curation.config import load_config
from mod_private.curation.__main__ import DEFAULT_CATEGORIES, main
from mod_private.curation.sources import own_note_items, tip_items
from mod_private.schemas import InboxEntry
from mod_private.store import PrivateStore

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
PACKAGE = Path(__file__).parents[2] / "modules/mod-private/mod_private/curation"


def tip(store: PrivateStore, summary: str) -> None:
    store.inbox.put(
        InboxEntry(
            entry_id=uuid4(),
            summary=summary,
            source_ref="https://www.instagram.com/reel/ABCDE12345/",
            received_at=NOW,
        )
    )


def test_shipped_categories_are_valid() -> None:
    config = load_config(DEFAULT_CATEGORIES)
    assert config.domain is Domain.PRIVATE


def test_sources_are_private_and_tips_stay_tainted(tmp_path: Path) -> None:
    store = PrivateStore(tmp_path)
    tip(store, "Go to bed at the same time every night. It stabilises sleep.")
    inbox = tmp_path / "curation/inbox"
    inbox.mkdir(parents=True)
    (inbox / "gym.md").write_text("Gym plan\n- Three sessions a week\n", encoding="utf-8")
    [t] = tip_items(store)
    [n] = own_note_items(inbox)
    assert t.classification is n.classification is DataClassification.PRIVATE
    assert t.domain is n.domain is Domain.PRIVATE
    assert t.taint is Taint.EXTERNAL_CONTENT and n.taint is Taint.NONE
    assert n.signal > t.signal


def test_cli_stores_own_notes_and_holds_tips(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = PrivateStore(tmp_path)
    tip(store, "Go to bed at the same time every night. It stabilises sleep.")
    inbox = tmp_path / "curation/inbox"
    inbox.mkdir(parents=True)
    (inbox / "sleep.md").write_text(
        "Evening routine\nsignal: 0.9\nNo screens after 22:00, read instead. #sleep\n"
        "- Phone stays in the kitchen\n- Lights dimmed at 21:30\n",
        encoding="utf-8",
    )
    base = ["--data-dir", str(tmp_path)]
    assert main([*base, "run"]) == 0
    counts = json.loads(capsys.readouterr().out)
    assert counts["collected"] == 2
    assert counts["stored"] == 1 and counts["review"] == 1  # own note in, external tip waits

    assert main([*base, "search", "screens evening"]) == 0
    assert "Evening routine" in capsys.readouterr().out
    assert main([*base, "report"]) == 0
    assert json.loads(capsys.readouterr().out)["audit_chain_intact"] is True


def test_private_curation_has_no_way_out() -> None:
    for file in PACKAGE.rglob("*.py"):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert not any(
            m.startswith(("jarvis_core.boundary", "jarvis_core.router", "jarvis_core.api"))
            for m in modules
        ), file.name
