from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from jarvis_core.contracts.actions import ActionRequest, ApprovalTier
from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.events import Taint
from jarvis_core.contracts.task_token import TaskTokenError, issue_token, verify_token
from jarvis_core.gate.action_gate import GateOutcome, decide

KEY = b"k" * 32
NOW = datetime(2026, 10, 16, 8, 0, tzinfo=UTC)


def _token(task_id: UUID | None = None, taint: Taint = Taint.NONE) -> str:
    return issue_token(
        KEY,
        task_id=task_id or uuid4(),
        domain=Domain.CONTENT,
        allowed_tools=frozenset({"publish_post", "save_draft"}),
        budget_eur=0.5,
        taint=taint,
        now=NOW,
    )


def test_token_roundtrip() -> None:
    claims = verify_token(KEY, _token(), now=NOW)
    assert claims.domain is Domain.CONTENT
    assert "publish_post" in claims.allowed_tools


def test_forged_token_is_rejected() -> None:
    token = _token()
    with pytest.raises(TaskTokenError):
        verify_token(b"x" * 32, token, now=NOW)


def test_expired_token_is_rejected() -> None:
    with pytest.raises(TaskTokenError, match="expired"):
        verify_token(KEY, _token(), now=NOW + timedelta(hours=1))


def _request(task_id: UUID, tool: str, tier: ApprovalTier) -> ActionRequest:
    return ActionRequest(
        task_id=task_id,
        domain=Domain.CONTENT,
        tool=tool,
        tier=tier,
        rationale="test",
    )


def test_gate_requires_approval_for_publishing() -> None:
    task_id = uuid4()
    claims = verify_token(KEY, _token(task_id=task_id), now=NOW)
    decision = decide(_request(task_id, "publish_post", ApprovalTier.C), claims)
    assert decision.outcome is GateOutcome.NEEDS_APPROVAL


def test_gate_rejects_tool_outside_token() -> None:
    task_id = uuid4()
    claims = verify_token(KEY, _token(task_id=task_id), now=NOW)
    decision = decide(_request(task_id, "delete_account", ApprovalTier.B), claims)
    assert decision.outcome is GateOutcome.REJECT


def test_tainted_task_needs_human_even_for_reversible_action() -> None:
    task_id = uuid4()
    claims = verify_token(KEY, _token(task_id=task_id, taint=Taint.EXTERNAL_CONTENT), now=NOW)
    decision = decide(_request(task_id, "save_draft", ApprovalTier.B), claims)
    assert decision.outcome is GateOutcome.NEEDS_APPROVAL


def test_untainted_reversible_action_runs() -> None:
    task_id = uuid4()
    claims = verify_token(KEY, _token(task_id=task_id), now=NOW)
    assert decide(_request(task_id, "save_draft", ApprovalTier.B), claims).outcome is (
        GateOutcome.EXECUTE
    )
