"""Trading curation: inbox ideas become PRIVATE research context in trading's own store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import Taint, TradingIdeaProposed
from jarvis_core.curation.config import load_config
from mod_trading.curation.__main__ import DEFAULT_CATEGORIES, main
from mod_trading.curation.sources import idea_items
from mod_trading.inbox import TradingInbox


def propose(
    inbox: TradingInbox, summary: str, instruments: tuple[str, ...] = (), reel: str = "ABCDE12345"
) -> None:
    inbox.receive(
        TradingIdeaProposed(
            origin_domain=Domain.CONTENT,
            target_domain=Domain.TRADING,
            classification=DataClassification.PUBLIC,
            taint=Taint.EXTERNAL_CONTENT,
            summary=summary,
            instruments=instruments,
            source_ref=f"https://www.instagram.com/reel/{reel}/",
        ),
        sender=Domain.CONTENT,
    )


def test_shipped_categories_are_valid() -> None:
    config = load_config(DEFAULT_CATEGORIES)
    assert config.domain is Domain.TRADING
    assert all(c.max_classification is DataClassification.PRIVATE for c in config.categories)


def test_watchlist_ideas_score_higher_and_fail_closed(tmp_path: Path) -> None:
    inbox = TradingInbox(tmp_path / "ideas.json")
    propose(inbox, "QQQ opening range breakout after the first 15 minutes.", ("QQQ",))
    propose(inbox, "Some penny stock will moon.")
    qqq, penny = idea_items(tmp_path / "ideas.json")
    assert qqq.signal > penny.signal
    assert "qqq" in qqq.tags
    assert qqq.classification is DataClassification.PRIVATE
    assert qqq.taint is Taint.EXTERNAL_CONTENT


def test_cli_curates_into_trading_store_with_review(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inbox = TradingInbox(tmp_path / "ideas.json")
    propose(inbox, "QQQ opening range breakout after the first 15 minutes.", ("QQQ",))
    propose(inbox, "QQQ opening range breakout after the first 15 minutes!", ("QQQ",), "QQQQQ12345")
    propose(inbox, "Some penny stock will moon.", reel="PENNY12345")
    base = ["--data-dir", str(tmp_path)]

    assert main([*base, "run"]) == 0
    counts = json.loads(capsys.readouterr().out)
    assert counts["collected"] == 3 and counts["duplicates_dropped"] == 1
    assert counts["stored"] == 0  # nothing from social media enters trading context unasked
    assert counts["review"] + counts["discarded"] == 2

    assert main([*base, "review"]) == 0
    out = capsys.readouterr().out
    assert "[index-etf]" in out
    brief = next(line.split()[0] for line in out.splitlines() if "[index-etf]" in line)
    assert main([*base, "decide", brief, "accept"]) == 0
    capsys.readouterr()
    assert main([*base, "context", "--category", "index-etf"]) == 0
    assert "QQQ opening range" in capsys.readouterr().out
    assert (tmp_path / "curation/context/index-etf.jsonl").exists()
