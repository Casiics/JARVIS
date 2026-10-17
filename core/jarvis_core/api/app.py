"""API gateway for the command deck (docs/architecture.md, sections 3.1, 7.1 and 9).

The deck observes and approves; it is not a privileged control channel. The only way to
decide a held ActionRequest is a passkey ceremony bound to that approval and that decision,
after which the gateway signs a single-use grant the executor checks
(jarvis_core.approvals.grant.authorize_execution).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from jarvis_core.api.auth import (
    OWNER,
    Ceremony,
    CeremonyKind,
    CeremonyStore,
    CredentialStore,
    Session,
    SessionStore,
    StoredCredential,
    approval_challenge,
    sha256_hex,
)
from jarvis_core.api.context_reviews import ContextReview, ContextReviewDesk, ReviewUnknown
from jarvis_core.api.reels import ReelDesk, ReelStatus, ReelSubmission, TooManyOpenReels
from jarvis_core.approvals.grant import (
    GRANT_TTL,
    ApprovalGrant,
    Decision,
    request_hash,
    sign_grant,
)
from jarvis_core.approvals.store import (
    ApprovalConflict,
    ApprovalStatus,
    ApprovalStore,
    PendingApproval,
)
from jarvis_core.contracts.audit import AuditAction, AuditDecision, AuditLogEntry
from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.reel_url import InvalidReelUrl
from jarvis_core.contracts.telemetry import TelemetryEvent
from jarvis_core.management.delegation import DelegationAgent, DelegationResult
from jarvis_core.orchestrator.service import Orchestrator, TaskRecord

SESSION_COOKIE = "jarvis_session"
AuditSink = Callable[[AuditLogEntry], object]


@dataclass(frozen=True)
class GatewaySettings:
    grant_key: bytes
    rp_id: str = "jarvis.home"
    rp_name: str = "JARVIS"
    origin: str = "https://jarvis.home"
    session_ttl: timedelta = timedelta(hours=8)
    ceremony_ttl: timedelta = timedelta(minutes=2)
    bootstrap_code_hash: str | None = None
    static_dir: Path | None = None
    secure_cookies: bool = True


class TelemetryHub:
    """Fan-out of metadata-only telemetry to connected decks. Slow clients drop events.

    publish() may be called from any thread; each subscriber's queue is fed on its own loop.
    """

    def __init__(self, queue_size: int = 256) -> None:
        self._subscribers: dict[asyncio.Queue[TelemetryEvent], asyncio.AbstractEventLoop] = {}
        self._size = queue_size

    def publish(self, event: TelemetryEvent) -> None:
        for queue, loop in list(self._subscribers.items()):
            loop.call_soon_threadsafe(_offer, queue, event)

    @contextlib.contextmanager
    def subscribe(self) -> Iterator[asyncio.Queue[TelemetryEvent]]:
        queue: asyncio.Queue[TelemetryEvent] = asyncio.Queue(self._size)
        self._subscribers[queue] = asyncio.get_running_loop()
        try:
            yield queue
        finally:
            self._subscribers.pop(queue, None)


def _offer(queue: asyncio.Queue[TelemetryEvent], event: TelemetryEvent) -> None:
    with contextlib.suppress(asyncio.QueueFull):
        queue.put_nowait(event)


@dataclass
class GatewayDeps:
    credentials: CredentialStore
    approvals: ApprovalStore
    audit: AuditSink
    sessions: SessionStore = field(default_factory=SessionStore)
    ceremonies: CeremonyStore = field(default_factory=CeremonyStore)
    telemetry: TelemetryHub = field(default_factory=TelemetryHub)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    bootstrap_used: bool = False
    reels: ReelDesk | None = None
    orchestrator: Orchestrator | None = None
    context_reviews: ContextReviewDesk | None = None
    delegation: DelegationAgent | None = None


# --- request bodies ---------------------------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterOptionsBody(_Body):
    bootstrap_code: str | None = Field(default=None, max_length=200)
    label: str = Field(default="", max_length=60)


class CeremonyBody(_Body):
    ceremony_id: str = Field(max_length=100)
    credential: dict[str, Any]


class ApprovalOptionsBody(_Body):
    decision: Decision


class ReelBody(_Body):
    url: str = Field(max_length=500)


class RequestBody(_Body):
    text: str = Field(min_length=1, max_length=2000)


class ContextDecisionBody(_Body):
    accept: bool
    note: str = Field(default="", max_length=300)


# --- app --------------------------------------------------------------------------------------


def create_app(settings: GatewaySettings, deps: GatewayDeps) -> FastAPI:
    if len(settings.grant_key) < 32:
        raise ValueError("grant_key must be at least 32 bytes")
    app = FastAPI(title="JARVIS API gateway", docs_url=None, redoc_url=None, openapi_url=None)

    def audit(target: str, decision: AuditDecision, **kw: Any) -> None:
        deps.audit(
            AuditLogEntry(
                trace_id=uuid4().hex,
                domain=Domain.CORE,
                actor=kw.pop("actor", "spiffe://jarvis.home/core/api-gateway"),
                action=kw.pop("action", AuditAction.AUTH),
                target=target,
                decision=decision,
                **kw,
            )
        )

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # State-changing requests must come from the deck's own origin as JSON (CSRF).
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("origin") != settings.origin:
                return JSONResponse({"detail": "origin not allowed"}, status_code=403)
            if not request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"detail": "JSON required"}, status_code=415)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def session_of(request: Request) -> Session:
        session = deps.sessions.get(request.cookies.get(SESSION_COOKIE), deps.clock())
        if session is None:
            raise HTTPException(401, "login required")
        return session

    def ceremony_deadline() -> datetime:
        return deps.clock() + settings.ceremony_ttl

    # --- registration --------------------------------------------------------------------

    @app.post("/auth/register/options")
    def register_options(body: RegisterOptionsBody, request: Request) -> dict[str, Any]:
        existing = deps.credentials.for_user(OWNER)
        bootstrap = False
        if existing:
            session_of(request)  # adding a device needs a signed-in owner
        else:
            supplied = sha256_hex(body.bootstrap_code or "")
            expected = settings.bootstrap_code_hash
            if (
                deps.bootstrap_used
                or expected is None
                or not secrets.compare_digest(supplied, expected)
            ):
                audit("auth.register", AuditDecision.DENY, policy_id="bootstrap code")
                raise HTTPException(403, "bootstrap code required")
            bootstrap = True
        options = generate_registration_options(
            rp_id=settings.rp_id,
            rp_name=settings.rp_name,
            user_name="owner",
            user_id=OWNER.encode(),
            user_display_name="JARVIS owner",
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=c.credential_id) for c in existing
            ],
        )
        ceremony_id = deps.ceremonies.put(
            Ceremony(
                CeremonyKind.REGISTER,
                options.challenge,
                ceremony_deadline(),
                bootstrap=bootstrap,
            )
        )
        return {"ceremony_id": ceremony_id, "options": json.loads(options_to_json(options))}

    @app.post("/auth/register/verify")
    def register_verify(body: CeremonyBody) -> dict[str, Any]:
        ceremony = deps.ceremonies.take(body.ceremony_id, CeremonyKind.REGISTER, deps.clock())
        if ceremony is None:
            raise HTTPException(400, "unknown or expired ceremony")
        if ceremony.bootstrap and deps.bootstrap_used:
            raise HTTPException(403, "bootstrap code already used")
        try:
            verified = verify_registration_response(
                credential=body.credential,
                expected_challenge=ceremony.challenge,
                expected_rp_id=settings.rp_id,
                expected_origin=settings.origin,
                require_user_verification=True,
            )
        except InvalidRegistrationResponse as exc:
            audit("auth.register", AuditDecision.DENY, policy_id="invalid attestation")
            raise HTTPException(400, "registration failed") from exc
        credential = StoredCredential(
            credential_id=verified.credential_id,
            public_key=verified.credential_public_key,
            sign_count=verified.sign_count,
            user_id=OWNER,
            created_at=deps.clock(),
        )
        deps.credentials.add(credential)
        if ceremony.bootstrap:
            deps.bootstrap_used = True
        audit("auth.register", AuditDecision.ALLOW, content_hash=credential.credential_hash)
        return {"registered": True}

    # --- login ---------------------------------------------------------------------------

    @app.post("/auth/login/options")
    def login_options() -> dict[str, Any]:
        options = generate_authentication_options(
            rp_id=settings.rp_id,
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=c.credential_id)
                for c in deps.credentials.for_user(OWNER)
            ],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        ceremony_id = deps.ceremonies.put(
            Ceremony(CeremonyKind.LOGIN, options.challenge, ceremony_deadline())
        )
        return {"ceremony_id": ceremony_id, "options": json.loads(options_to_json(options))}

    @app.post("/auth/login/verify")
    def login_verify(body: CeremonyBody, response: Response) -> dict[str, Any]:
        ceremony = deps.ceremonies.take(body.ceremony_id, CeremonyKind.LOGIN, deps.clock())
        if ceremony is None:
            raise HTTPException(400, "unknown or expired ceremony")
        credential = _assert(deps, settings, body.credential, ceremony.challenge, audit, "login")
        token, _ = deps.sessions.create(
            OWNER, credential.credential_hash, deps.clock(), settings.session_ttl
        )
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=int(settings.session_ttl.total_seconds()),
            httponly=True,
            secure=settings.secure_cookies,
            samesite="strict",
            path="/",
        )
        audit("auth.login", AuditDecision.ALLOW, content_hash=credential.credential_hash)
        return {"user": OWNER}

    @app.post("/auth/logout")
    def logout(request: Request, response: Response) -> dict[str, Any]:
        deps.sessions.revoke(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"logged_out": True}

    # --- deck API ------------------------------------------------------------------------

    @app.get("/api/me")
    def me(request: Request) -> dict[str, Any]:
        session = session_of(request)
        return {"user": session.user_id, "expires_at": session.expires_at.isoformat()}

    @app.get("/api/attention")
    def attention(request: Request) -> dict[str, Any]:
        session_of(request)
        now = deps.clock()
        items = [_summary(a, now) for a in deps.approvals.pending()]
        reviews = 0
        if deps.context_reviews is not None:
            deps.context_reviews.sync()
            reviews = len(deps.context_reviews.open())
        return {"approvals": items, "count": len(items), "context_reviews": reviews}

    @app.get("/api/approvals/{approval_id}")
    def approval_detail(approval_id: UUID, request: Request) -> dict[str, Any]:
        session_of(request)
        approval = deps.approvals.get(approval_id)
        if approval is None:
            raise HTTPException(404, "unknown approval")
        return _detail(approval, deps.clock())

    @app.post("/api/approvals/{approval_id}/options")
    def approval_options(
        approval_id: UUID, body: ApprovalOptionsBody, request: Request
    ) -> dict[str, Any]:
        session = session_of(request)
        approval = deps.approvals.get(approval_id)
        if approval is None:
            raise HTTPException(404, "unknown approval")
        if approval.status is not ApprovalStatus.PENDING:
            raise HTTPException(409, "approval already decided")
        digest = request_hash(approval.request)
        options = generate_authentication_options(
            rp_id=settings.rp_id,
            challenge=approval_challenge(approval_id, body.decision, digest),
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=c.credential_id)
                for c in deps.credentials.for_user(session.user_id)
            ],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        ceremony_id = deps.ceremonies.put(
            Ceremony(
                CeremonyKind.APPROVAL,
                options.challenge,
                ceremony_deadline(),
                session_hash=session.token_hash,
                approval_id=approval_id,
                decision=body.decision,
                request_hash=digest,
            )
        )
        return {"ceremony_id": ceremony_id, "options": json.loads(options_to_json(options))}

    @app.post("/api/approvals/{approval_id}/verify")
    def approval_verify(approval_id: UUID, body: CeremonyBody, request: Request) -> dict[str, Any]:
        session = session_of(request)
        ceremony = deps.ceremonies.take(body.ceremony_id, CeremonyKind.APPROVAL, deps.clock())
        if (
            ceremony is None
            or ceremony.approval_id != approval_id
            or ceremony.session_hash != session.token_hash
            or ceremony.decision is None
        ):
            raise HTTPException(400, "unknown or expired ceremony")
        approval = deps.approvals.get(approval_id)
        if approval is None or request_hash(approval.request) != ceremony.request_hash:
            raise HTTPException(409, "the request changed; start again")
        credential = _assert(deps, settings, body.credential, ceremony.challenge, audit, "approval")
        now = deps.clock()
        grant = ApprovalGrant(
            approval_id=approval_id,
            request_hash=ceremony.request_hash,
            decision=ceremony.decision,
            approver=session.user_id,
            credential_hash=credential.credential_hash,
            issued_at=now,
            expires_at=now + GRANT_TTL,
        )
        token = sign_grant(settings.grant_key, grant)
        try:
            decided = deps.approvals.decide(
                approval_id, ceremony.decision, session.user_id, token, now
            )
        except ApprovalConflict as exc:
            raise HTTPException(409, "approval already decided") from exc
        audit(
            f"approval:{approval.request.tool}",
            AuditDecision.ALLOW if ceremony.decision is Decision.APPROVE else AuditDecision.DENY,
            action=AuditAction.APPROVAL,
            actor=f"user:{session.user_id}",
            task_id=approval.request.task_id,
            approval_ref=str(approval_id),
            taint=approval.request.taint,
            content_hash=ceremony.request_hash,
        )
        return {"approval_id": str(approval_id), "status": decided.status.value}

    # --- reel intake ---------------------------------------------------------------------

    def reel_desk() -> ReelDesk:
        if deps.reels is None:
            raise HTTPException(503, "reel intake is not configured")
        return deps.reels

    @app.get("/api/content/reels")
    def list_reels(request: Request) -> dict[str, Any]:
        session_of(request)
        desk = reel_desk()
        desk.sync()
        return {"reels": [_reel(s) for s in desk.all()]}

    @app.post("/api/content/reels")
    def submit_reel(body: ReelBody, request: Request, response: Response) -> dict[str, Any]:
        session = session_of(request)
        desk = reel_desk()
        try:
            submission, created = desk.submit(body.url)
        except InvalidReelUrl as exc:
            raise HTTPException(422, "Das ist kein Instagram-Reel-Link.") from exc
        except TooManyOpenReels as exc:
            raise HTTPException(429, "Zu viele Reels warten noch. Bitte später.") from exc
        if created:
            audit(
                "content:reel_submitted",
                AuditDecision.ALLOW,
                action=AuditAction.BOUNDARY_EVENT,
                actor=f"user:{session.user_id}",
                task_id=submission.task_id,
                content_hash=sha256_hex(submission.source_ref),
            )
        response.status_code = 201 if created else 200
        return {"reel": _reel(submission), "created": created}

    @app.post("/api/content/reels/retry")
    def retry_reels(request: Request) -> dict[str, Any]:
        """Queue every failed reel again (e.g. after a fix in the content worker)."""
        session = session_of(request)
        desk = reel_desk()
        desk.sync()
        retried = desk.retry_failed()
        for submission in retried:
            audit(
                "content:reel_retried",
                AuditDecision.ALLOW,
                action=AuditAction.BOUNDARY_EVENT,
                actor=f"user:{session.user_id}",
                task_id=submission.task_id,
                content_hash=sha256_hex(submission.source_ref),
            )
        still_failed = sum(s.status is ReelStatus.FAILED for s in desk.all())
        return {"retried": len(retried), "still_failed": still_failed}

    # --- orchestrator ---------------------------------------------------------------------

    @app.get("/api/tasks")
    def list_tasks(request: Request) -> dict[str, Any]:
        session_of(request)
        if deps.orchestrator is None:
            raise HTTPException(503, "the orchestrator is not configured")
        deps.orchestrator.tick()
        return {"tasks": [_task(t) for t in deps.orchestrator.tasks()[:100]]}

    # --- management: requests in plain words -----------------------------------------------

    @app.post("/api/requests")
    def delegate_request(body: RequestBody, request: Request) -> dict[str, Any]:
        """The delegation agent picks the specialised agent; the orchestrator dispatches."""
        session = session_of(request)
        if deps.delegation is None:
            raise HTTPException(503, "the delegation agent is not configured")
        result = deps.delegation.delegate(body.text, actor=f"user:{session.user_id}")
        audit(
            f"core:delegation:{result.status.value}",
            AuditDecision.ALLOW if result.task_id else AuditDecision.ESCALATE,
            action=AuditAction.BOUNDARY_EVENT,
            actor=f"user:{session.user_id}",
            task_id=result.task_id,
            content_hash=sha256_hex(body.text),
        )
        return {"result": _delegation(result)}

    # --- context reviews ------------------------------------------------------------------

    def review_desk() -> ContextReviewDesk:
        if deps.context_reviews is None:
            raise HTTPException(503, "context reviews are not configured")
        return deps.context_reviews

    @app.get("/api/content/context-reviews")
    def list_context_reviews(request: Request) -> dict[str, Any]:
        session_of(request)
        desk = review_desk()
        desk.sync()
        return {
            "open": [_context_review(r) for r in desk.open()],
            "recent": [_context_review(r) for r in desk.recent()[:10]],
        }

    @app.post("/api/content/context-reviews/{brief_id}/decision")
    def decide_context_review(
        brief_id: str, body: ContextDecisionBody, request: Request
    ) -> dict[str, Any]:
        session = session_of(request)
        desk = review_desk()
        try:
            review = desk.decide(brief_id, body.accept, f"user:{session.user_id}", body.note)
        except ReviewUnknown as exc:
            raise HTTPException(409, "Diese Freigabe ist nicht mehr offen.") from exc
        audit(
            "content:context_review",
            AuditDecision.ALLOW if body.accept else AuditDecision.DENY,
            action=AuditAction.APPROVAL,
            actor=f"user:{session.user_id}",
            content_ref=brief_id,
            taint=review.request.taint,
        )
        return {"review": _context_review(review)}

    # --- telemetry -----------------------------------------------------------------------

    @app.websocket("/ws/telemetry")
    async def telemetry(ws: WebSocket) -> None:
        if ws.headers.get("origin") != settings.origin:
            await ws.close(code=4403)
            return
        if deps.sessions.get(ws.cookies.get(SESSION_COOKIE), deps.clock()) is None:
            await ws.close(code=4401)
            return
        await ws.accept()
        with deps.telemetry.subscribe() as queue:
            try:
                while True:
                    event = await queue.get()
                    await ws.send_text(event.model_dump_json())
            except WebSocketDisconnect:
                return

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if settings.static_dir is not None:
        _mount_deck(app, settings.static_dir)
    return app


def _assert(
    deps: GatewayDeps,
    settings: GatewaySettings,
    raw: dict[str, Any],
    challenge: bytes,
    audit: Callable[..., None],
    purpose: str,
) -> StoredCredential:
    from webauthn.helpers import base64url_to_bytes

    try:
        credential_id = base64url_to_bytes(str(raw.get("rawId") or raw.get("id") or ""))
    except ValueError as exc:
        raise HTTPException(400, "malformed credential") from exc
    stored = deps.credentials.get(credential_id)
    if stored is None or stored.user_id != OWNER:
        audit(f"auth.{purpose}", AuditDecision.DENY, policy_id="unknown credential")
        raise HTTPException(401, "unknown credential")
    try:
        verified = verify_authentication_response(
            credential=raw,
            expected_challenge=challenge,
            expected_rp_id=settings.rp_id,
            expected_origin=settings.origin,
            credential_public_key=stored.public_key,
            credential_current_sign_count=stored.sign_count,
            require_user_verification=True,
        )
    except InvalidAuthenticationResponse as exc:
        audit(f"auth.{purpose}", AuditDecision.DENY, policy_id="invalid assertion")
        raise HTTPException(401, "passkey verification failed") from exc
    deps.credentials.update_sign_count(stored.credential_id, verified.new_sign_count)
    return stored


def _age_s(approval: PendingApproval, now: datetime) -> int:
    return max(int((now - approval.created_at).total_seconds()), 0)


def _summary(approval: PendingApproval, now: datetime) -> dict[str, Any]:
    req = approval.request
    return {
        "approval_id": str(approval.approval_id),
        "domain": req.domain.value,
        "tool": req.tool,
        "tier": req.tier.value,
        "tainted": req.taint.value != "none",
        "gate_outcome": approval.gate_outcome.value,
        "age_s": _age_s(approval, now),
    }


def _detail(approval: PendingApproval, now: datetime) -> dict[str, Any]:
    req = approval.request
    detail = _summary(approval, now) | {
        "status": approval.status.value,
        "gate_reason": approval.gate_reason,
        "task_id": str(req.task_id),
        "request_hash": request_hash(req),
    }
    # The private view shows metadata only; content stays in the private domain.
    if req.domain is not Domain.PRIVATE:
        detail |= {"rationale": req.rationale, "parameters": req.parameters}
    return detail


def _reel(submission: ReelSubmission) -> dict[str, Any]:
    return {
        "task_id": str(submission.task_id),
        "reel_code": submission.reel_code,
        "source_ref": submission.source_ref,
        "status": submission.status.value,
        "submitted_at": submission.submitted_at.isoformat(),
        "updated_at": submission.updated_at.isoformat(),
        "duration_ms": submission.duration_ms,
        "error_class": submission.error_class,
    }


def _delegation(result: DelegationResult) -> dict[str, Any]:
    return {
        "status": result.status.value,
        "route": result.route,
        "domain": result.domain.value if result.domain else None,
        "task_id": str(result.task_id) if result.task_id else None,
        "reason": result.reason,
        "error_class": result.error_class,
        "used_model": result.used_model,
    }


def _task(record: TaskRecord) -> dict[str, Any]:
    """Metadata only: the goal itself stays behind its hash."""
    return {
        "task_id": str(record.task_id),
        "kind": record.kind,
        "domain": record.domain.value,
        "state": record.state.value,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "duration_ms": record.duration_ms,
        "error_class": record.error_class,
        "cost_eur": round(record.cost_eur, 6),
    }


def _context_review(review: ContextReview) -> dict[str, Any]:
    req = review.request
    return {
        "brief_id": req.brief_id,
        "category": req.category,
        "proposal": req.proposal,
        "confidence": req.confidence,
        "title": req.title,
        "summary": req.summary,
        "facts": list(req.facts),
        "reasons": list(req.reasons),
        "flags": list(req.flags),
        "source_refs": list(req.source_refs),
        "received_at": review.received_at.isoformat(),
        "decided_at": review.decided_at.isoformat() if review.decided_at else None,
        "accepted": review.accepted,
    }


def _mount_deck(app: FastAPI, root: Path) -> None:
    root = root.resolve()
    index = root / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    def deck(path: str) -> Response:
        if path.startswith(("api/", "auth/", "ws/")):
            raise HTTPException(404)
        candidate = (root / path).resolve()
        if path and candidate.is_file() and root in candidate.parents:
            return FileResponse(candidate)
        if index.is_file():
            return FileResponse(index)  # client-side routes and deep links
        raise HTTPException(404)
