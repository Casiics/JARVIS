"""--watch keeps running when one pass fails and retries it at the next poll."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.curation.cli import DomainCli, run_cli
from jarvis_core.curation.models import CollectedItem
from jarvis_core.curation.similarity import fingerprint

CATEGORIES = "domain: content\ndefault_category: general\ncategories:\n  - name: general\n"


def test_watch_survives_a_failing_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = {"sources": 0, "sleeps": 0}

    def sources(_data: Path, _root: Path) -> Iterator[CollectedItem]:
        calls["sources"] += 1
        if calls["sources"] == 1:
            raise ValueError("secret note text must not be printed")
        yield CollectedItem(
            item_id="note:1",
            domain=Domain.CONTENT,
            classification=DataClassification.PUBLIC,
            source_ref="note:1",
            collected_at=datetime(2026, 10, 17, tzinfo=UTC),
            title="t",
            summary="ripgrep is fast",
            signal=0.9,
            content_hash=fingerprint("ripgrep is fast"),
        )

    def sleep(_seconds: float) -> None:
        calls["sleeps"] += 1
        if calls["sleeps"] == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", sleep)
    categories = tmp_path / "categories.yaml"
    categories.write_text(CATEGORIES, encoding="utf-8")
    cli = DomainCli(
        prog="test",
        domain="content",
        actor="spiffe://jarvis.home/content/test",
        default_data=tmp_path / "data",
        default_categories=categories,
        sources=sources,
        watched=lambda data, _root: (data / "inputs.txt",),
    )
    assert run_cli(cli, ["run", "--watch", "--poll-seconds", "0"]) == 0
    out = capsys.readouterr()
    assert calls["sources"] == 2  # the failed pass was retried although no input changed
    assert '{"error": "ValueError"}' in out.err and "secret" not in out.err
    assert '"stored":1' in out.out


def test_without_watch_a_failure_still_raises(tmp_path: Path) -> None:
    def sources(_data: Path, _root: Path) -> Iterator[CollectedItem]:
        raise ValueError("boom")
        yield  # pragma: no cover

    categories = tmp_path / "categories.yaml"
    categories.write_text(CATEGORIES, encoding="utf-8")
    cli = DomainCli(
        prog="test",
        domain="content",
        actor="spiffe://jarvis.home/content/test",
        default_data=tmp_path / "data",
        default_categories=categories,
        sources=sources,
        watched=lambda data, _root: (),
    )
    with pytest.raises(ValueError, match="boom"):
        run_cli(cli, ["run"])
