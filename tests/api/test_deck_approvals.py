"""Phase 3 exit criterion: a tier C action can only be approved via the deck.

The deck's passkey ceremony is the only way to obtain a grant, and the execution guard
accepts nothing but a valid, matching, unused, fresh grant.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from jarvis_core.api.app import SESSION_COOKIE, GatewayDeps, GatewaySettings, create_app
from jarvis_core.api.auth import InMemoryCredentialStore, sha256_hex
from jarvis_core.api.filestore import FileApprovalStore, FileCredentialStore
from jarvis_core.approvals.grant import (
    ApprovalGrant,
    Decision,
    GrantError,
    InMemoryUsedGrants,
    authorize_execution,
    request_hash,
    sign_grant,
)
from jarvis_core.approvals.store import ApprovalStatus, InMemoryApprovalStore, hold
from jarvis_core.contracts.actions import ActionRequest, ApprovalTier
from jarvis_core.contracts.audit import AuditAction, AuditLogEntry
from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.events import Taint
from jarvis_core.contracts.task_token import TaskTokenClaims
from jarvis_core.contracts.telemetry import TelemetryEvent, TelemetryState
from jarvis_core.gate.action_gate import GateDecision, GateOutcome, decide
from tests.api.softauthn import SoftAuthenticator

ORIGIN = "https://jarvis.home"
RP_ID = "jarvis.home"
KEY = b"g" * 32
CODE = "bootstrap-code-for-tests"
HEADERS = {"Origin": ORIGIN}


def _publish_request(domain: Domain = Domain.CONTENT) -> tuple[ActionRequest, GateDecision]:
    request = ActionRequest(
        task_id=uuid4(),
        domain=domain,
        tool="publish_post",
        parameters={"post_id": "p1"},
        tier=ApprovalTier.C,
        rationale="publish the approved reel summary",
    )
    claims = TaskTokenClaims(
        task_id=request.task_id,
        domain=domain,
        allowed_tools=frozenset({"publish_post"}),
        budget_eur=0,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    return request, decide(request, claims)


class Deck:
    def __init__(self, tmp_path: Path | None = None) -> None:
        self.audit: list[AuditLogEntry] = []
        self.approvals = (
            FileApprovalStore(tmp_path / "a.json") if tmp_path else InMemoryApprovalStore()
        )
        self.deps = GatewayDeps(
            credentials=FileCredentialStore(tmp_path / "c.json")
            if tmp_path
            else InMemoryCredentialStore(),
            approvals=self.approvals,
            audit=self.audit.append,
        )
        self.settings = GatewaySettings(
            grant_key=KEY, rp_id=RP_ID, origin=ORIGIN, bootstrap_code_hash=sha256_hex(CODE)
        )
        self.client = TestClient(create_app(self.settings, self.deps), base_url=ORIGIN)
        self.key = SoftAuthenticator(RP_ID, ORIGIN)

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self.client.post(path, json=body or {}, headers=HEADERS)

    def register(self) -> None:
        opts = self.post("/auth/register/options", {"bootstrap_code": CODE}).json()
        res = self.post(
            "/auth/register/verify",
            {"ceremony_id": opts["ceremony_id"], "credential": self.key.register(opts["options"])},
        )
        assert res.status_code == 200, res.text

    def login(self) -> None:
        opts = self.post("/auth/login/options").json()
        res = self.post(
            "/auth/login/verify",
            {"ceremony_id": opts["ceremony_id"], "credential": self.key.assert_(opts["options"])},
        )
        assert res.status_code == 200, res.text

    def decide(self, approval_id: str, decision: str = "approve") -> Any:
        opts = self.post(f"/api/approvals/{approval_id}/options", {"decision": decision})
        assert opts.status_code == 200, opts.text
        body = opts.json()
        return self.post(
            f"/api/approvals/{approval_id}/verify",
            {"ceremony_id": body["ceremony_id"], "credential": self.key.assert_(body["options"])},
        )


@pytest.fixture
def deck() -> Deck:
    d = Deck()
    d.register()
    d.login()
    return d


# --- the exit criterion -----------------------------------------------------------------------


def test_tier_c_cannot_execute_without_a_deck_grant() -> None:
    request, decision = _publish_request()
    assert decision.outcome is GateOutcome.NEEDS_APPROVAL
    with pytest.raises(GrantError, match="approval from the deck"):
        authorize_execution(request, decision, None, key=KEY, used=InMemoryUsedGrants())


def test_tier_c_executes_after_passkey_approval_in_the_deck(deck: Deck) -> None:
    request, decision = _publish_request()
    approval = hold(deck.approvals, request, decision)

    res = deck.decide(str(approval.approval_id))
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "approved"

    stored = deck.approvals.get(approval.approval_id)
    assert stored is not None and stored.grant_token is not None
    used = InMemoryUsedGrants()
    grant = authorize_execution(request, decision, stored.grant_token, key=KEY, used=used)
    assert grant is not None and grant.approver == "owner"

    # Single use: the same grant cannot run the action twice.
    with pytest.raises(GrantError, match="already used"):
        authorize_execution(request, decision, stored.grant_token, key=KEY, used=used)

    approvals = [e for e in deck.audit if e.action is AuditAction.APPROVAL]
    assert len(approvals) == 1
    assert approvals[0].approval_ref == str(approval.approval_id)
    assert "publish the approved" not in approvals[0].model_dump_json()


def test_rejection_in_the_deck_blocks_execution(deck: Deck) -> None:
    request, decision = _publish_request()
    approval = hold(deck.approvals, request, decision)
    assert deck.decide(str(approval.approval_id), "reject").json()["status"] == "rejected"
    stored = deck.approvals.get(approval.approval_id)
    assert stored is not None
    with pytest.raises(GrantError, match="rejected"):
        authorize_execution(
            request, decision, stored.grant_token, key=KEY, used=InMemoryUsedGrants()
        )


def test_grant_does_not_cover_a_changed_request(deck: Deck) -> None:
    request, decision = _publish_request()
    approval = hold(deck.approvals, request, decision)
    deck.decide(str(approval.approval_id))
    stored = deck.approvals.get(approval.approval_id)
    assert stored is not None
    changed = request.model_copy(update={"parameters": {"post_id": "p2"}})
    with pytest.raises(GrantError, match="different request"):
        authorize_execution(
            changed, decision, stored.grant_token, key=KEY, used=InMemoryUsedGrants()
        )


def test_forged_and_expired_grants_are_refused() -> None:
    request, decision = _publish_request()
    now = datetime.now(UTC)
    grant = ApprovalGrant(
        approval_id=uuid4(),
        request_hash=request_hash(request),
        decision=Decision.APPROVE,
        approver="owner",
        credential_hash="0" * 64,
        issued_at=now - timedelta(minutes=20),
        expires_at=now - timedelta(minutes=10),
    )
    with pytest.raises(GrantError, match="signature"):
        authorize_execution(
            request, decision, sign_grant(b"x" * 32, grant), key=KEY, used=InMemoryUsedGrants()
        )
    with pytest.raises(GrantError, match="expired"):
        authorize_execution(
            request, decision, sign_grant(KEY, grant), key=KEY, used=InMemoryUsedGrants()
        )
    with pytest.raises(GrantError, match="malformed"):
        authorize_execution(request, decision, "garbage", key=KEY, used=InMemoryUsedGrants())


def test_gate_rejections_cannot_be_overridden_by_a_grant() -> None:
    request, _ = _publish_request()
    rejected = GateDecision(GateOutcome.REJECT, "forbidden")
    with pytest.raises(GrantError, match="rejected"):
        authorize_execution(request, rejected, "anything", key=KEY, used=InMemoryUsedGrants())


def test_tier_a_runs_without_a_grant() -> None:
    request, _ = _publish_request()
    assert (
        authorize_execution(
            request.model_copy(update={"tier": ApprovalTier.A}),
            GateDecision(GateOutcome.EXECUTE, "tier A"),
            None,
            key=KEY,
            used=InMemoryUsedGrants(),
        )
        is None
    )


def test_only_held_requests_can_be_queued() -> None:
    request, _ = _publish_request()
    with pytest.raises(ValueError, match="does not need approval"):
        hold(InMemoryApprovalStore(), request, GateDecision(GateOutcome.EXECUTE, "auto"))


# --- the ceremony cannot be bypassed ----------------------------------------------------------


def test_approval_needs_a_session(deck: Deck) -> None:
    approval = hold(deck.approvals, *_publish_request())
    deck.client.cookies.clear()
    res = deck.post(f"/api/approvals/{approval.approval_id}/options", {"decision": "approve"})
    assert res.status_code == 401


def test_login_assertion_cannot_be_replayed_as_approval(deck: Deck) -> None:
    approval = hold(deck.approvals, *_publish_request())
    login = deck.post("/auth/login/options").json()
    replay = deck.key.assert_(login["options"])
    opts = deck.post(f"/api/approvals/{approval.approval_id}/options", {"decision": "approve"})
    res = deck.post(
        f"/api/approvals/{approval.approval_id}/verify",
        {"ceremony_id": opts.json()["ceremony_id"], "credential": replay},
    )
    assert res.status_code == 401
    assert deck.approvals.get(approval.approval_id).status is ApprovalStatus.PENDING  # type: ignore[union-attr]


def test_ceremony_for_one_approval_cannot_decide_another(deck: Deck) -> None:
    first = hold(deck.approvals, *_publish_request())
    second = hold(deck.approvals, *_publish_request())
    opts = deck.post(f"/api/approvals/{first.approval_id}/options", {"decision": "approve"}).json()
    res = deck.post(
        f"/api/approvals/{second.approval_id}/verify",
        {"ceremony_id": opts["ceremony_id"], "credential": deck.key.assert_(opts["options"])},
    )
    assert res.status_code == 400
    assert deck.approvals.get(second.approval_id).status is ApprovalStatus.PENDING  # type: ignore[union-attr]


def test_ceremony_is_single_use_and_decision_is_final(deck: Deck) -> None:
    approval = hold(deck.approvals, *_publish_request())
    opts = deck.post(
        f"/api/approvals/{approval.approval_id}/options", {"decision": "approve"}
    ).json()
    body = {"ceremony_id": opts["ceremony_id"], "credential": deck.key.assert_(opts["options"])}
    assert deck.post(f"/api/approvals/{approval.approval_id}/verify", body).status_code == 200
    assert deck.post(f"/api/approvals/{approval.approval_id}/verify", body).status_code == 400
    again = deck.post(f"/api/approvals/{approval.approval_id}/options", {"decision": "reject"})
    assert again.status_code == 409


def test_wrong_origin_and_unverified_user_are_refused(deck: Deck) -> None:
    approval = hold(deck.approvals, *_publish_request())
    opts = deck.post(
        f"/api/approvals/{approval.approval_id}/options", {"decision": "approve"}
    ).json()
    phished = deck.key.assert_(opts["options"], origin="https://jarvis.home.evil.example")
    res = deck.post(
        f"/api/approvals/{approval.approval_id}/verify",
        {"ceremony_id": opts["ceremony_id"], "credential": phished},
    )
    assert res.status_code == 401

    deck.key.user_verified = False
    opts = deck.post(
        f"/api/approvals/{approval.approval_id}/options", {"decision": "approve"}
    ).json()
    res = deck.post(
        f"/api/approvals/{approval.approval_id}/verify",
        {"ceremony_id": opts["ceremony_id"], "credential": deck.key.assert_(opts["options"])},
    )
    assert res.status_code == 401


def test_cross_site_requests_are_refused(deck: Deck) -> None:
    approval = hold(deck.approvals, *_publish_request())
    path = f"/api/approvals/{approval.approval_id}/options"
    evil = deck.client.post(
        path, json={"decision": "approve"}, headers={"Origin": "https://evil.example"}
    )
    assert evil.status_code == 403
    form = deck.client.post(
        path,
        content="decision=approve",
        headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert form.status_code == 415


def test_no_other_route_changes_approvals(deck: Deck) -> None:
    mutating = {
        route.path
        for route in deck.client.app.routes  # type: ignore[attr-defined]
        if set(getattr(route, "methods", None) or ()) - {"GET", "HEAD"}
    }
    assert {p for p in mutating if "approval" in p} == {
        "/api/approvals/{approval_id}/options",
        "/api/approvals/{approval_id}/verify",
    }


# --- registration and sessions ----------------------------------------------------------------


def test_first_passkey_needs_the_bootstrap_code() -> None:
    d = Deck()
    assert d.post("/auth/register/options", {"bootstrap_code": "wrong"}).status_code == 403
    assert d.post("/auth/register/options").status_code == 403


def test_bootstrap_code_works_once_then_new_devices_need_a_session() -> None:
    d = Deck()
    d.register()
    assert d.post("/auth/register/options", {"bootstrap_code": CODE}).status_code == 401
    d.login()
    assert d.post("/auth/register/options").status_code == 200


def test_session_cookie_is_hardened(deck: Deck) -> None:
    opts = deck.post("/auth/login/options").json()
    res = deck.post(
        "/auth/login/verify",
        {"ceremony_id": opts["ceremony_id"], "credential": deck.key.assert_(opts["options"])},
    )
    cookie = res.headers["set-cookie"].lower()
    assert SESSION_COOKIE in cookie
    for attr in ("httponly", "secure", "samesite=strict"):
        assert attr in cookie
    assert "frame-ancestors 'none'" in res.headers["content-security-policy"]


def test_logout_ends_the_session(deck: Deck) -> None:
    assert deck.client.get("/api/me").status_code == 200
    deck.post("/auth/logout")
    assert deck.client.get("/api/me").status_code == 401


def test_private_domain_approvals_show_metadata_only(deck: Deck) -> None:
    request, decision = _publish_request(Domain.PRIVATE)
    approval = hold(deck.approvals, request, decision)
    detail = deck.client.get(f"/api/approvals/{approval.approval_id}").json()
    assert "rationale" not in detail and "parameters" not in detail
    assert detail["domain"] == "private"
    attention = deck.client.get("/api/attention").json()
    assert attention["count"] == 1
    assert "publish the approved" not in json.dumps(attention)


def test_tainted_requests_show_as_tainted(deck: Deck) -> None:
    request, decision = _publish_request()
    tainted = request.model_copy(update={"taint": Taint.EXTERNAL_CONTENT})
    hold(deck.approvals, tainted, decision)
    assert deck.client.get("/api/attention").json()["approvals"][0]["tainted"] is True


def test_passkeys_and_approvals_survive_a_restart(tmp_path: Path) -> None:
    d = Deck(tmp_path)
    d.register()
    approval = hold(d.approvals, *_publish_request())
    restarted = Deck(tmp_path)
    restarted.key = d.key
    restarted.login()
    assert restarted.decide(str(approval.approval_id)).json()["status"] == "approved"
    assert oct((tmp_path / "c.json").stat().st_mode & 0o777) == "0o600"


# --- telemetry --------------------------------------------------------------------------------


def test_telemetry_needs_a_session_and_the_deck_origin(deck: Deck) -> None:
    event = TelemetryEvent(
        task_id=uuid4(), domain=Domain.CONTENT, node="reel.extract", state=TelemetryState.RUNNING
    )
    # The test client does not send Secure cookies over ws://, so pass the session explicitly.
    session = {"Cookie": f"{SESSION_COOKIE}={deck.client.cookies.get(SESSION_COOKIE)}"}
    with deck.client.websocket_connect("/ws/telemetry", headers=HEADERS | session) as ws:
        deck.deps.telemetry.publish(event)
        assert TelemetryEvent.model_validate_json(ws.receive_text()) == event

    from starlette.websockets import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect),
        deck.client.websocket_connect(
            "/ws/telemetry", headers={"Origin": "https://evil.example"}
        ) as ws,
    ):
        ws.receive_text()
    deck.client.cookies.clear()
    with (
        pytest.raises(WebSocketDisconnect),
        deck.client.websocket_connect("/ws/telemetry", headers=HEADERS) as ws,
    ):
        ws.receive_text()


def test_telemetry_events_cannot_carry_content() -> None:
    with pytest.raises(ValueError):
        TelemetryEvent.model_validate(
            {
                "task_id": str(uuid4()),
                "domain": "content",
                "node": "n",
                "state": "running",
                "prompt": "hi",
            }
        )
    with pytest.raises(ValueError):
        TelemetryEvent(
            task_id=uuid4(),
            domain=Domain.CONTENT,
            node="free text with spaces",
            state=TelemetryState.DONE,
        )
