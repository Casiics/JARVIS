"""Ideas reach trading only through the boundary policy and never become orders."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import LifeTipProposed, Taint, TradingIdeaProposed
from mod_trading.inbox import FlowRejected, TradingInbox


def idea(**overrides: object) -> TradingIdeaProposed:
    base: dict[str, object] = {
        "origin_domain": Domain.CONTENT,
        "target_domain": Domain.TRADING,
        "classification": DataClassification.INTERNAL,
        "taint": Taint.EXTERNAL_CONTENT,
        "summary": "Momentum in European small caps",
        "instruments": ("VWCE.DE",),
        "source_ref": "reel:abc",
    }
    base.update(overrides)
    return TradingIdeaProposed.model_validate(base)


def test_content_idea_is_stored_with_its_taint(tmp_path: Path) -> None:
    inbox = TradingInbox(tmp_path / "ideas.json")
    stored = inbox.receive(idea(), sender=Domain.CONTENT)
    assert stored.taint is Taint.EXTERNAL_CONTENT
    assert [i.summary for i in inbox.all()] == ["Momentum in European small caps"]
    inbox.dismiss(stored.idea_id)
    assert inbox.all()[0].dismissed


def test_spoofed_origin_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(FlowRejected):
        TradingInbox(tmp_path / "i.json").receive(idea(), sender=Domain.PRIVATE)


def test_private_domain_cannot_send_ideas(tmp_path: Path) -> None:
    with pytest.raises(FlowRejected):
        TradingInbox(tmp_path / "i.json").receive(
            idea(origin_domain=Domain.PRIVATE), sender=Domain.PRIVATE
        )


def test_other_events_and_duplicates_are_rejected(tmp_path: Path) -> None:
    inbox = TradingInbox(tmp_path / "i.json")
    tip = LifeTipProposed(
        origin_domain=Domain.CONTENT,
        target_domain=Domain.PRIVATE,
        classification=DataClassification.INTERNAL,
        summary="x",
        source_ref="y",
    )
    with pytest.raises(FlowRejected):
        inbox.receive(tip, sender=Domain.CONTENT)
    event = idea()
    inbox.receive(event, sender=Domain.CONTENT)
    with pytest.raises(FlowRejected):
        inbox.receive(event, sender=Domain.CONTENT)
