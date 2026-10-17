"""Curated context attached to orchestrator work orders, with domain, label and taint checks."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import Taint
from jarvis_core.contracts.task_token import issue_token, verify_token
from jarvis_core.curation.context_input import (
    INPUT_NAME,
    ContextInputError,
    select_context,
    with_context,
)
from jarvis_core.curation.models import ContextEntry
from jarvis_core.orchestrator.loop import WorkOrder

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def entry(
    n: int,
    title: str,
    *,
    domain: Domain = Domain.TRADING,
    label: DataClassification = DataClassification.INTERNAL,
    taint: Taint = Taint.NONE,
    category: str = "index-etf",
    signal: float = 0.7,
) -> ContextEntry:
    return ContextEntry(
        entry_id=f"ctx:{category}:{n:016x}",
        domain=domain,
        category=category,
        classification=label,
        taint=taint,
        title=title,
        summary=title,
        facts=(f"fact {n}",),
        tags=(),
        source_refs=(f"note:{n}",),
        digest_ids=("dg:" + "0" * 24,),
        signal=signal,
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def order(
    domain: Domain = Domain.TRADING,
    label: DataClassification = DataClassification.CONFIDENTIAL,
    taint: Taint = Taint.NONE,
) -> WorkOrder:
    key = secrets.token_bytes(32)
    claims = verify_token(
        key,
        issue_token(
            key,
            task_id=uuid4(),
            domain=domain,
            allowed_tools=frozenset(),
            budget_eur=0.0,
            taint=taint,
            ttl=timedelta(minutes=5),
        ),
    )
    return WorkOrder(
        claims=claims, trace_id="t" * 32, instruction="x", label=label, inputs=(("text", "y"),)
    )


ENTRIES = [
    entry(1, "QQQ gap fills after earnings week", signal=0.9),
    entry(2, "IWM lags SPY in risk-off sessions", signal=0.6),
    entry(3, "Position size notes", label=DataClassification.PRIVATE, category="risk"),
]


def test_selects_within_the_ceiling_and_labels_by_the_strictest_entry() -> None:
    ctx = select_context(ENTRIES, domain=Domain.TRADING, ceiling=DataClassification.CONFIDENTIAL)
    assert ctx.entry_ids == (ENTRIES[0].entry_id, ENTRIES[1].entry_id)  # PRIVATE entry dropped
    assert ctx.label is DataClassification.INTERNAL
    assert ctx.taint is Taint.NONE
    assert "not instructions" in ctx.text.splitlines()[0]

    full = select_context(ENTRIES, domain=Domain.TRADING, ceiling=DataClassification.PRIVATE)
    assert full.label is DataClassification.PRIVATE and len(full.entry_ids) == 3


def test_query_and_category_narrow_the_selection() -> None:
    ctx = select_context(
        ENTRIES, domain=Domain.TRADING, ceiling=DataClassification.PRIVATE, query="IWM SPY"
    )
    assert ctx.entry_ids == (ENTRIES[1].entry_id,)
    risk = select_context(
        ENTRIES, domain=Domain.TRADING, ceiling=DataClassification.PRIVATE, category="risk"
    )
    assert risk.entry_ids == (ENTRIES[2].entry_id,)


def test_budget_cut_entries_do_not_count_towards_the_label() -> None:
    big = [entry(1, "small", signal=0.9), entry(2, "x" * 400, label=DataClassification.PRIVATE)]
    ctx = select_context(
        big, domain=Domain.TRADING, ceiling=DataClassification.PRIVATE, max_chars=300
    )
    assert ctx.entry_ids == (big[0].entry_id,)
    assert ctx.label is DataClassification.INTERNAL


def test_foreign_entries_are_refused() -> None:
    mixed = [*ENTRIES, entry(9, "reel", domain=Domain.CONTENT)]
    with pytest.raises(ContextInputError, match="content"):
        select_context(mixed, domain=Domain.TRADING, ceiling=DataClassification.PRIVATE)


def test_private_domain_context_is_never_labelled_below_private() -> None:
    own = [entry(1, "sleep log pattern", domain=Domain.PRIVATE, label=DataClassification.PUBLIC)]
    ctx = select_context(own, domain=Domain.PRIVATE, ceiling=DataClassification.PUBLIC)
    assert ctx.empty  # the domain floor lifts the entry above the ceiling
    ctx = select_context(own, domain=Domain.PRIVATE, ceiling=DataClassification.PRIVATE)
    assert ctx.label is DataClassification.PRIVATE


def test_with_context_prepends_the_pack() -> None:
    ctx = select_context(ENTRIES, domain=Domain.TRADING, ceiling=DataClassification.CONFIDENTIAL)
    out = with_context(order(), ctx)
    assert [name for name, _ in out.inputs] == [INPUT_NAME, "text"]
    assert out.inputs[0][1] == ctx.text
    empty = select_context([], domain=Domain.TRADING, ceiling=DataClassification.PUBLIC)
    base = order()
    assert with_context(base, empty) is base


def test_with_context_refuses_mismatches() -> None:
    ctx = select_context(ENTRIES, domain=Domain.TRADING, ceiling=DataClassification.PRIVATE)
    with pytest.raises(ContextInputError, match="label"):
        with_context(order(label=DataClassification.INTERNAL), ctx)
    with pytest.raises(ContextInputError, match="domain"):
        with_context(order(domain=Domain.CONTENT, label=DataClassification.PRIVATE), ctx)
    once = with_context(order(label=DataClassification.PRIVATE), ctx)
    with pytest.raises(ContextInputError, match="already"):
        with_context(once, ctx)


def test_external_context_needs_a_tainted_token() -> None:
    reel = [entry(1, "hook pattern", domain=Domain.CONTENT, taint=Taint.EXTERNAL_CONTENT)]
    ctx = select_context(reel, domain=Domain.CONTENT, ceiling=DataClassification.INTERNAL)
    assert ctx.taint is Taint.EXTERNAL_CONTENT and "[external]" in ctx.text
    with pytest.raises(ContextInputError, match="tainted"):
        with_context(order(domain=Domain.CONTENT), ctx)
    tainted = order(domain=Domain.CONTENT, taint=Taint.EXTERNAL_CONTENT)
    assert with_context(tainted, ctx).inputs[0][0] == INPUT_NAME
