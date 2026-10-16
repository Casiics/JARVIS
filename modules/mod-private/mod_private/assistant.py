"""The private domain's assistant: sync, triage, daily briefing and reply drafts.

Every step is checked against the task token, every loop runs through the TaskMachine
(max 4 iterations, Tier 0b before the local supervisor) and is audited with hashes and
counts only. Saving a reply draft is an external effect: it becomes an ActionRequest that
carries only the draft's id and hash, so the approval queue in core never sees its text.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from functools import partial
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel

from jarvis_core.approvals.grant import UsedGrants, authorize_execution
from jarvis_core.contracts.actions import ActionRequest, ApprovalTier
from jarvis_core.contracts.audit import AuditAction, AuditDecision, AuditLogEntry
from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.events import BoundaryEvent, LifeTipProposed, Taint
from jarvis_core.contracts.task_token import TaskTokenClaims
from jarvis_core.gate.action_gate import GateDecision, GateOutcome, decide
from jarvis_core.orchestrator.state_machine import Event, Task, TaskMachine, TaskState
from jarvis_core.policy.flows import check_flow
from jarvis_core.router.client import ModelClientError
from jarvis_core.router.gateway import GatewayResult
from jarvis_core.router.policy import RoutingDenied
from mod_private.gate import GateResult, check_briefing, check_reply, check_triage
from mod_private.schemas import (
    Briefing,
    CalendarEvent,
    DraftStatus,
    InboxEntry,
    MailDraft,
    MailMessage,
    MailTriage,
)
from mod_private.store import PrivateStore, SyncState
from mod_private.supervisor import LocalSupervisor, QaVerdict
from mod_private.workers import PrivateWorkers

TOOL_CALENDAR = "calendar.read"
TOOL_MAIL = "mail.read"
TOOL_TRIAGE = "model.triage"
TOOL_BRIEFING = "model.briefing"
TOOL_REPLY = "model.reply"
TOOL_SAVE_DRAFT = "mail.save_draft"
PRIVATE_TOOLS = frozenset(
    {TOOL_CALENDAR, TOOL_MAIL, TOOL_TRIAGE, TOOL_BRIEFING, TOOL_REPLY, TOOL_SAVE_DRAFT}
)

ACTOR = "spiffe://jarvis.home/private/assistant"
CALENDAR_PAST = timedelta(days=1)
CALENDAR_AHEAD = timedelta(days=14)


class CalendarSource(Protocol):
    def events(self, start: datetime, end: datetime) -> list[CalendarEvent]: ...


class MailSource(Protocol):
    def fetch_new(
        self, after_uid: int, uidvalidity: str | None
    ) -> tuple[str, list[MailMessage]]: ...

    def append_draft(self, message: bytes) -> None: ...


class ToolNotAllowed(Exception):
    pass


class NotFound(Exception):
    pass


@dataclass(frozen=True)
class SyncReport:
    events: int
    new_mails: int


@dataclass(frozen=True)
class LoopOutcome[T]:
    task: Task
    value: T | None
    flags: tuple[str, ...]


@dataclass(frozen=True)
class DraftProposal:
    draft: MailDraft
    request: ActionRequest
    decision: GateDecision


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class PrivateAssistant:
    store: PrivateStore
    workers: PrivateWorkers
    supervisor: LocalSupervisor
    audit: Callable[[AuditLogEntry], object]
    calendar: CalendarSource | None = None
    mailbox: MailSource | None = None
    owner_address: str = ""
    tz: tzinfo = UTC
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    # --- sync ---------------------------------------------------------------------------------

    def sync(self, claims: TaskTokenClaims) -> SyncReport:
        trace = uuid4().hex
        events = 0
        if self.calendar is not None:
            self._allow(claims, TOOL_CALENDAR)
            now = self.clock()
            fetched = self.calendar.events(now - CALENDAR_PAST, now + CALENDAR_AHEAD)
            self.store.events.put_many(fetched)
            events = len(fetched)
            self._tool_audit(claims, trace, f"{TOOL_CALENDAR}:{events}", AuditDecision.ALLOW)
        new_mails = 0
        if self.mailbox is not None:
            self._allow(claims, TOOL_MAIL)
            state = self.store.sync_state()
            validity, mails = self.mailbox.fetch_new(state.last_uid, state.uidvalidity)
            self.store.mails.put_many(mails)
            last = max((m.uid for m in mails), default=0)
            if validity == state.uidvalidity:
                last = max(last, state.last_uid)
            self.store.save_sync_state(SyncState(uidvalidity=validity, last_uid=last))
            new_mails = len(mails)
            self._tool_audit(claims, trace, f"{TOOL_MAIL}:{new_mails}", AuditDecision.ALLOW)
        return SyncReport(events, new_mails)

    # --- triage -------------------------------------------------------------------------------

    def triage_new(self, claims: TaskTokenClaims) -> list[MailTriage]:
        """Sort new mail. Tier 0b checks each answer; the supervisor reviews the briefing."""
        self._allow(claims, TOOL_TRIAGE)
        today = self.clock().astimezone(self.tz).date()
        results: list[MailTriage] = []
        for mail in self.store.untriaged():
            outcome = self._loop(
                claims,
                attempt=partial(self._triage_attempt, mail),
                check=lambda raw: check_triage(raw, today),
                review=None,
            )
            if outcome.value is None:
                continue
            triage = MailTriage(
                mail_id=mail.mail_id,
                category=outcome.value.category,
                summary=outcome.value.summary,
                deadline=outcome.value.deadline,
                suspicious=outcome.value.suspicious or bool(outcome.flags),
                injection_flags=outcome.flags,
            )
            self.store.triage.put(triage)
            results.append(triage)
        return results

    def _triage_attempt(
        self, mail: MailMessage, feedback: tuple[str, ...], task: Task
    ) -> GatewayResult:
        return self.workers.triage(mail, feedback, task_id=task.task_id, trace_id=task.trace_id)

    # --- briefing -----------------------------------------------------------------------------

    def briefing(self, claims: TaskTokenClaims, day: date) -> Briefing | None:
        self._allow(claims, TOOL_BRIEFING)
        start = datetime.combine(day, time.min, self.tz)
        end = start + timedelta(days=1)
        events = [e for e in self.store.events.all() if _overlaps(e, start, end, self.tz)]
        events.sort(key=lambda e: _start_of(e, self.tz))
        triage = self._relevant_triage(day, start)
        inbox = [i for i in self.store.inbox.all() if not i.dismissed]
        sources = _sources_text(events, triage, inbox)

        outcome = self._loop(
            claims,
            attempt=lambda fb, task: self.workers.briefing(
                day,
                events,
                triage,
                inbox,
                self.tz,
                fb,
                task_id=task.task_id,
                trace_id=task.trace_id,
            ),
            check=lambda raw: check_briefing(
                raw, events=events, triage=triage, inbox=inbox, tz=self.tz
            ),
            review=lambda draft, task: self.supervisor.review(
                f"daily briefing for {day.isoformat()}",
                draft.model_dump_json(),
                sources,
                task_id=task.task_id,
                trace_id=task.trace_id,
            ),
        )
        if outcome.value is None:
            return None
        briefing = Briefing(
            day=day,
            headline=outcome.value.headline,
            items=tuple(outcome.value.items),
            created_at=self.clock(),
            iterations=outcome.task.iterations,
        )
        self.store.briefings.put(briefing)
        return briefing

    def _relevant_triage(self, day: date, start: datetime) -> list[MailTriage]:
        since = start - timedelta(days=1)
        out: list[MailTriage] = []
        for triage in self.store.triage.all():
            mail = self.store.mails.get(triage.mail_id)
            recent = mail is not None and mail.received_at is not None and mail.received_at >= since
            due = triage.deadline is not None and day <= triage.deadline <= day + timedelta(days=7)
            if recent or due:
                out.append(triage)
        return out

    # --- reply drafts -------------------------------------------------------------------------

    def propose_reply(
        self, claims: TaskTokenClaims, mail_id: str, instruction: str
    ) -> DraftProposal | None:
        """Write a reply draft and ask to save it. Nothing is sent, ever."""
        self._allow(claims, TOOL_REPLY)
        mail = self.store.mails.get(mail_id)
        if mail is None:
            raise NotFound("unknown mail")
        outcome = self._loop(
            claims,
            attempt=lambda fb, task: self.workers.reply(
                mail, instruction, fb, task_id=task.task_id, trace_id=task.trace_id
            ),
            check=lambda raw: check_reply(raw, mail, instruction),
            review=lambda draft, task: self.supervisor.review(
                f"reply to an email; owner's instruction: {instruction[:300]}",
                draft.model_dump_json(),
                f"From: {mail.sender}\nSubject: {mail.subject}\n\n{mail.body}",
                task_id=task.task_id,
                trace_id=task.trace_id,
            ),
            with_action=True,
        )
        if outcome.value is None:
            return None
        subject = mail.subject if mail.subject.lower().startswith("re:") else f"Re: {mail.subject}"
        draft = MailDraft(
            in_reply_to=mail.message_id,
            to=(_address(mail.sender),),
            subject=subject[:500],
            body=outcome.value.body,
            created_at=self.clock(),
        )
        self.store.drafts.put(draft)
        request = ActionRequest(
            task_id=claims.task_id,
            domain=Domain.PRIVATE,
            tool=TOOL_SAVE_DRAFT,
            parameters={"draft_id": str(draft.draft_id), "content_sha256": _draft_hash(draft)},
            tier=ApprovalTier.B,
            taint=Taint.EXTERNAL_CONTENT,
            rationale="Save a reply draft in the mailbox's drafts folder (never sent).",
        )
        decision = decide(request, claims)
        self._tool_audit(
            claims,
            outcome.task.trace_id,
            f"{TOOL_SAVE_DRAFT}:{decision.outcome.value}",
            AuditDecision.DENY
            if decision.outcome is GateOutcome.REJECT
            else AuditDecision.ESCALATE,
            content_hash=_draft_hash(draft),
        )
        return DraftProposal(draft, request, decision)

    def save_draft(
        self,
        claims: TaskTokenClaims,
        request: ActionRequest,
        grant_token: str | None,
        *,
        grant_key: bytes,
        used: UsedGrants,
    ) -> MailDraft:
        """Executor for mail.save_draft: runs only with a valid single-use grant."""
        if self.mailbox is None:
            raise NotFound("no mailbox configured")
        if request.tool != TOOL_SAVE_DRAFT:
            raise ToolNotAllowed(request.tool)
        decision = decide(request, claims)
        authorize_execution(request, decision, grant_token, key=grant_key, used=used)
        draft = self.store.draft(UUID(str(request.parameters["draft_id"])))
        if draft is None or draft.status is not DraftStatus.PROPOSED:
            raise NotFound("draft is gone or already handled")
        if _draft_hash(draft) != request.parameters.get("content_sha256"):
            raise ToolNotAllowed("the draft changed after it was approved")
        self.mailbox.append_draft(self._render(draft))
        saved = draft.model_copy(update={"status": DraftStatus.SAVED})
        self.store.drafts.put(saved)
        self._tool_audit(
            claims,
            uuid4().hex,
            TOOL_SAVE_DRAFT,
            AuditDecision.ALLOW,
            content_hash=_draft_hash(draft),
        )
        return saved

    def _render(self, draft: MailDraft) -> bytes:
        message = EmailMessage()
        if self.owner_address:
            message["From"] = self.owner_address
        message["To"] = ", ".join(draft.to)
        message["Subject"] = draft.subject
        message["Date"] = formatdate(localtime=False)
        message["Message-ID"] = make_msgid(domain="jarvis.invalid")
        if draft.in_reply_to:
            message["In-Reply-To"] = draft.in_reply_to
            message["References"] = draft.in_reply_to
        message.set_content(draft.body)
        return message.as_bytes()

    # --- inbox --------------------------------------------------------------------------------

    def receive(self, event: BoundaryEvent, *, sender: Domain) -> InboxEntry:
        """Accept a proposal from another domain. Only LifeTipProposed, only via the policy."""
        flow = check_flow(event, caller_domain=sender)
        if not flow.allowed or event.target_domain is not Domain.PRIVATE:
            raise ToolNotAllowed(f"refused: {flow.reason}")
        if not isinstance(event, LifeTipProposed):
            raise ToolNotAllowed("the private inbox takes LifeTipProposed only")
        entry = InboxEntry(
            entry_id=event.event_id,
            summary=event.summary,
            source_ref=event.source_ref,
            received_at=self.clock(),
        )
        self.store.inbox.put(entry)
        return entry

    def dismiss(self, entry_id: UUID) -> None:
        entry = self.store.inbox.get(str(entry_id))
        if entry is None:
            raise NotFound("unknown inbox entry")
        self.store.inbox.put(entry.model_copy(update={"dismissed": True}))

    # --- shared loop --------------------------------------------------------------------------

    def _loop[V: BaseModel](
        self,
        claims: TaskTokenClaims,
        *,
        attempt: Callable[[tuple[str, ...], Task], GatewayResult],
        check: Callable[[str], GateResult[V]],
        review: Callable[[V, Task], tuple[QaVerdict, GatewayResult]] | None,
        with_action: bool = False,
    ) -> LoopOutcome[V]:
        machine = TaskMachine(self.audit, ACTOR)
        task = machine.apply(
            Task(task_id=claims.task_id, domain=Domain.PRIVATE, trace_id=uuid4().hex), Event.START
        )
        feedback: tuple[str, ...] = ()
        while task.state is TaskState.RUNNING:
            try:
                raw = attempt(feedback, task).text
            except (RoutingDenied, ModelClientError):
                task = machine.apply(task, Event.STEP_FAILED)
                break
            task = machine.apply(task, Event.WORKER_FINISHED)
            result = check(raw)
            if not result.passed or result.value is None:
                feedback = result.errors
                task = machine.apply(task, Event.GATE_FAILED)
                continue
            task = machine.apply(task, Event.GATE_PASSED)
            if review is not None:
                try:
                    verdict, _ = review(result.value, task)
                except (RoutingDenied, ModelClientError):
                    task = machine.apply(task, Event.STEP_FAILED)
                    break
                if not verdict.accept:
                    feedback = verdict.feedback or ("the supervisor rejected the draft",)
                    task = machine.apply(task, Event.QA_REJECTED)
                    continue
            event = Event.QA_ACCEPTED_WITH_ACTION if with_action else Event.QA_ACCEPTED
            task = machine.apply(task, event)
            return LoopOutcome(task, result.value, result.flags)
        return LoopOutcome(task, None, ())

    def _allow(self, claims: TaskTokenClaims, tool: str) -> None:
        if claims.domain is not Domain.PRIVATE or tool not in claims.allowed_tools:
            raise ToolNotAllowed(tool)
        if claims.expires_at <= self.clock():
            raise ToolNotAllowed("task token expired")

    def _tool_audit(
        self,
        claims: TaskTokenClaims,
        trace: str,
        target: str,
        decision: AuditDecision,
        *,
        content_hash: str | None = None,
    ) -> None:
        self.audit(
            AuditLogEntry(
                trace_id=trace,
                task_id=claims.task_id,
                domain=Domain.PRIVATE,
                actor=ACTOR,
                action=AuditAction.TOOL_CALL,
                target=target[:200],
                decision=decision,
                taint=Taint.EXTERNAL_CONTENT,
                content_hash=content_hash,
            )
        )


def _draft_hash(draft: MailDraft) -> str:
    return sha256_text(f"{draft.in_reply_to}\0{','.join(draft.to)}\0{draft.subject}\0{draft.body}")


def _address(sender: str) -> str:
    return parseaddr(sender)[1] or sender


def _start_of(event: CalendarEvent, tz: tzinfo) -> datetime:
    if isinstance(event.start, datetime):
        return event.start
    return datetime.combine(event.start, time.min, tz)


def _end_of(event: CalendarEvent, tz: tzinfo) -> datetime:
    if isinstance(event.end, datetime):
        return event.end
    if event.end is not None:
        return datetime.combine(event.end, time.min, tz)
    if isinstance(event.start, datetime):
        return event.start
    return datetime.combine(event.start, time.min, tz) + timedelta(days=1)


def _overlaps(event: CalendarEvent, start: datetime, end: datetime, tz: tzinfo) -> bool:
    begin, finish = _start_of(event, tz), _end_of(event, tz)
    if finish == begin:
        return start <= begin < end
    return begin < end and finish > start


def _sources_text(
    events: list[CalendarEvent], triage: list[MailTriage], inbox: list[InboxEntry]
) -> str:
    lines = [f"event {e.uid}: {e.start} {e.summary} {e.location}" for e in events]
    lines += [
        f"mail {t.mail_id}: {t.category.value} {t.deadline or ''} {t.summary}" for t in triage
    ]
    lines += [f"tip {i.entry_id}: {i.summary}" for i in inbox]
    return "\n".join(lines) or "(no sources)"
