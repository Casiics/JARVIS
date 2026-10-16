"""Fakes for the private domain: a scripted local model, a calendar and an IMAP server."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from typing import Any

from mod_private.schemas import CalendarEvent, MailMessage

from jarvis_core.router.client import ModelCall, ModelResult

KINDS = {
    "You sort one email": "triage",
    "You write the owner's daily briefing": "briefing",
    "You draft a reply": "reply",
    "You review a draft": "supervisor",
}


class ScriptedModel:
    """Answers by prompt kind, in order; records every call."""

    def __init__(self, **answers: list[str]) -> None:
        self.answers = {k: list(v) for k, v in answers.items()}
        self.calls: list[tuple[str, ModelCall]] = []

    def complete(self, call: ModelCall) -> ModelResult:
        system = call.messages[0].content
        kind = next(v for k, v in KINDS.items() if system.startswith(k))
        self.calls.append((kind, call))
        queue = self.answers.get(kind, [])
        text = queue.pop(0) if queue else "not json"
        return ModelResult(text=text, prompt_tokens=100, completion_tokens=20)

    def loaded_models(self) -> list[str]:
        return []

    def unload(self, model: str) -> None:
        return None

    def kinds(self) -> list[str]:
        return [k for k, _ in self.calls]


class ExplodingCloud:
    def complete(self, call: ModelCall) -> ModelResult:
        raise AssertionError("a private-domain call reached the cloud client")

    def loaded_models(self) -> list[str]:
        return []

    def unload(self, model: str) -> None:
        return None


@dataclass
class FakeCalendar:
    items: list[CalendarEvent]
    calls: list[tuple[datetime, datetime]] = field(default_factory=list)

    def events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        self.calls.append((start, end))
        return list(self.items)


@dataclass
class FakeMailSource:
    messages: list[MailMessage]
    validity: str = "7"
    appended: list[bytes] = field(default_factory=list)

    def fetch_new(self, after_uid: int, uidvalidity: str | None) -> tuple[str, list[MailMessage]]:
        if uidvalidity is not None and uidvalidity != self.validity:
            after_uid = 0
        return self.validity, [m for m in self.messages if m.uid > after_uid]

    def append_draft(self, message: bytes) -> None:
        self.appended.append(message)


def rfc822(
    subject: str, body: str, *, sender: str = "Ann <ann@example.org>", html: bool = False
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "owner@example.org"
    msg["Subject"] = subject
    msg["Date"] = "Fri, 16 Oct 2026 07:30:00 +0200"
    msg["Message-ID"] = "<m1@example.org>"
    if html:
        msg.set_content(body, subtype="html")
    else:
        msg.set_content(body)
    return msg.as_bytes()


class FakeImap:
    """Implements the ImapConnection subset over an in-memory mailbox."""

    def __init__(self, messages: dict[int, bytes], validity: str = "7") -> None:
        self.messages = messages
        self.validity = validity
        self.log: list[tuple[Any, ...]] = []
        self.appended: list[tuple[str | None, str | None, bytes]] = []
        self.logged_in = False

    def login(self, user: str, password: str) -> tuple[str, Any]:
        self.log.append(("login", user))
        self.logged_in = password == "pw"  # noqa: S105 - test fake
        return ("OK" if self.logged_in else "NO"), [b""]

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, Any]:
        self.log.append(("select", mailbox, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code: str) -> tuple[str, Any]:
        return code, [self.validity.encode()]

    def uid(self, command: str, *args: str) -> tuple[str, Any]:
        self.log.append(("uid", command, *args))
        if command == "SEARCH":
            low = int(args[0].split()[1].split(":")[0])
            hits = [u for u in sorted(self.messages) if u >= low] or [max(self.messages)]
            return "OK", [" ".join(str(u) for u in hits).encode()]
        uid = int(args[0])
        raw = self.messages[uid]
        if args[1] == "(RFC822.SIZE)":
            return "OK", [f"1 (UID {uid} RFC822.SIZE {len(raw)})".encode()]
        assert args[1] == "(BODY.PEEK[])", "only PEEK reads are allowed"
        return "OK", [(f"1 (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]

    def append(
        self, mailbox: str | None, flags: str | None, date_time: Any, message: bytes
    ) -> tuple[str, Any]:
        self.appended.append((mailbox, flags, message))
        return "OK", [b""]

    def logout(self) -> tuple[str, Any]:
        self.log.append(("logout",))
        return "BYE", [b""]
