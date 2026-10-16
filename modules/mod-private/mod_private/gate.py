"""Tier 0b for the private domain: deterministic checks on every worker answer.

Failures go back to the worker without a supervisor call. Error texts are fixed strings and
never quote the answer or the sources. Beyond the schema, the gate checks that a briefing
only cites sources it was given, keeps their times, and introduces no links, mail addresses
or phone numbers of its own: an injected mail cannot smuggle a link into the briefing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from pydantic import BaseModel, ValidationError

from jarvis_core.quarantine import has_control_chars, injection_flags, parse_json_object
from mod_private.schemas import (
    BriefingDraft,
    CalendarEvent,
    InboxEntry,
    MailMessage,
    MailTriage,
    ReplyDraftDraft,
    TriageDraft,
)

_URL = re.compile(r"\b(?:https?://|www\.)[^\s<>\"')\]]+", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_PHONE = re.compile(r"(?<![\w:])\+?\d[\d ()/-]{7,}\d(?![\w:])")
_CLOCK = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b")


@dataclass(frozen=True)
class GateResult[T: BaseModel]:
    value: T | None
    errors: tuple[str, ...]
    flags: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.value is not None and not self.errors


def _parse[T: BaseModel](raw: str, model: type[T]) -> GateResult[T]:
    try:
        data = parse_json_object(raw)
    except ValueError:
        return GateResult(None, ("answer is not a single JSON object",))
    try:
        return GateResult(model.model_validate(data), ())
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) or "root" for e in exc.errors()})
        return GateResult(None, (f"schema violation in: {', '.join(fields)}",))


def contacts(text: str) -> set[str]:
    """Links, mail addresses and phone numbers in a text, normalised for comparison."""
    found = {m.group(0).lower().rstrip(".,;:") for m in _URL.finditer(text)}
    found |= {m.group(0).lower() for m in _EMAIL.finditer(text)}
    digits = (re.sub(r"\D", "", m.group(0)) for m in _PHONE.finditer(text))
    found |= {d for d in digits if len(d) >= 9}  # shorter runs are dates and amounts
    return found


def check_triage(raw: str, today: date) -> GateResult[TriageDraft]:
    result = _parse(raw, TriageDraft)
    draft = result.value
    if draft is None:
        return result
    errors: list[str] = []
    if has_control_chars(draft.summary):
        errors.append("control characters are not allowed")
    if draft.deadline is not None and not (
        today - timedelta(days=31) <= draft.deadline <= today + timedelta(days=730)
    ):
        errors.append("deadline is outside the plausible range")
    if errors:
        return GateResult(None, tuple(errors))
    return GateResult(draft, (), injection_flags(draft.summary))


def check_briefing(
    raw: str,
    *,
    events: list[CalendarEvent],
    triage: list[MailTriage],
    inbox: list[InboxEntry],
    tz: tzinfo,
) -> GateResult[BriefingDraft]:
    result = _parse(raw, BriefingDraft)
    draft = result.value
    if draft is None:
        return result
    by_uid = {e.uid: e for e in events}
    by_mail = {t.mail_id: t for t in triage}
    tips = {str(i.entry_id): i for i in inbox}
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    texts = [draft.headline, *(i.text for i in draft.items)]
    if any(has_control_chars(t) for t in texts):
        errors.append("control characters are not allowed")
    for item in draft.items:
        if (item.kind, item.ref) in seen:
            errors.append("an item is listed twice")
        seen.add((item.kind, item.ref))
        if item.kind == "event":
            event = by_uid.get(item.ref)
            if event is None:
                errors.append("an event item cites an unknown uid")
            elif not _times_match(item.text, event, tz):
                errors.append("an event item states a time the event does not have")
        elif item.kind == "mail" and item.ref not in by_mail:
            errors.append("a mail item cites an unknown mail id")
        elif item.kind == "reminder" and item.ref not in by_mail and item.ref not in tips:
            if item.ref not in by_uid:
                errors.append("a reminder cites an unknown source")
    source_text = "\n".join(
        [*(f"{e.summary} {e.location} {e.description}" for e in events)]
        + [t.summary for t in triage]
        + [i.summary for i in inbox]
    )
    if contacts("\n".join(texts)) - contacts(source_text):
        errors.append("links, mail addresses or phone numbers must come from the sources")
    if errors:
        return GateResult(None, tuple(dict.fromkeys(errors)))
    return GateResult(draft, (), injection_flags("\n".join(texts)))


def _times_match(text: str, event: CalendarEvent, tz: tzinfo) -> bool:
    stated = {(int(h), int(m)) for h, m in _CLOCK.findall(text)}
    if not stated:
        return True
    allowed: set[tuple[int, int]] = set()
    for moment in (event.start, event.end):
        if isinstance(moment, datetime):
            local = moment.astimezone(tz)
            allowed.add((local.hour, local.minute))
    return stated <= allowed


def check_reply(raw: str, mail: MailMessage, instruction: str) -> GateResult[ReplyDraftDraft]:
    result = _parse(raw, ReplyDraftDraft)
    draft = result.value
    if draft is None:
        return result
    errors: list[str] = []
    if has_control_chars(draft.body):
        errors.append("control characters are not allowed")
    if contacts(draft.body) - contacts(instruction) - contacts(mail.sender):
        errors.append("links, mail addresses or phone numbers must come from the instruction")
    if errors:
        return GateResult(None, tuple(errors))
    return GateResult(draft, (), injection_flags(draft.body))
