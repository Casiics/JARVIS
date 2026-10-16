"""IMAP access for the private domain: read new mail without marking it, save drafts.

Reading uses EXAMINE (read-only) and BODY.PEEK, so nothing on the server changes. The only
write is APPEND to the drafts folder, and only the draft executor calls it after the action
gate and a human approval. Nothing is ever sent: there is no SMTP in this domain.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import html
import imaplib
import re
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any, Protocol

from mod_private.schemas import MailMessage

MAX_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_BODY_CHARS = 20_000
MAX_BATCH = 50
_TAGS = re.compile(r"<[^>]+>")
_UID_LINE = re.compile(rb"UID (\d+)")
_SIZE = re.compile(rb"RFC822\.SIZE (\d+)")


class ImapError(Exception):
    pass


class ImapConnection(Protocol):
    """The subset of imaplib.IMAP4 the private domain uses."""

    def login(self, user: str, password: str) -> tuple[str, Any]: ...

    def select(self, mailbox: str = ..., readonly: bool = ...) -> tuple[str, Any]: ...

    def uid(self, command: str, *args: str) -> tuple[str, Any]: ...

    def append(
        self, mailbox: str | None, flags: str | None, date_time: Any, message: bytes
    ) -> tuple[str, Any]: ...

    def response(self, code: str) -> tuple[str, Any]: ...

    def logout(self) -> tuple[str, Any]: ...


def tls_connection(host: str, port: int) -> ImapConnection:
    return imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=30)


def text_body(message: EmailMessage) -> tuple[str, bool]:
    """Plain text of a message, falling back to stripped HTML. Returns (text, truncated)."""
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return "", False
    try:
        content = part.get_content()
    except (LookupError, ValueError):
        return "", False
    text = str(content)
    if part.get_content_type() == "text/html":
        text = html.unescape(_TAGS.sub(" ", text))
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text[:MAX_BODY_CHARS], len(text) > MAX_BODY_CHARS


def parse_message(raw: bytes, *, mail_id: str, uid: int) -> MailMessage:
    parsed = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(parsed, EmailMessage)
    body, truncated = text_body(parsed)
    received: datetime | None = None
    if parsed["Date"]:
        try:
            received = email.utils.parsedate_to_datetime(str(parsed["Date"]))
            if received.tzinfo is None:
                received = received.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            received = None
    to = tuple(addr for _, addr in email.utils.getaddresses([str(parsed.get("To", ""))]) if addr)
    return MailMessage(
        mail_id=mail_id,
        uid=uid,
        message_id=str(parsed.get("Message-ID", ""))[:500],
        sender=str(parsed.get("From", ""))[:500],
        to=to[:20],
        subject=str(parsed.get("Subject", ""))[:500],
        received_at=received,
        body=body,
        truncated=truncated,
    )


@dataclass
class ImapMailbox:
    host: str
    username: str
    password: str = field(repr=False)
    port: int = 993
    mailbox: str = "INBOX"
    drafts_mailbox: str = "Drafts"
    connect: Callable[[str, int], ImapConnection] = tls_connection

    def fetch_new(self, after_uid: int, uidvalidity: str | None) -> tuple[str, list[MailMessage]]:
        """Messages with a UID above `after_uid`, oldest first, at most MAX_BATCH.

        Returns the mailbox's UIDVALIDITY; when it differs from the one passed in, UIDs were
        reset on the server and the caller starts over from zero.
        """
        conn = self._open()
        try:
            status, _ = conn.select(self.mailbox, readonly=True)
            if status != "OK":
                raise ImapError("mailbox not available")
            validity = _first(conn.response("UIDVALIDITY")[1]) or "0"
            if uidvalidity is not None and validity != uidvalidity:
                after_uid = 0
            status, data = conn.uid("SEARCH", f"UID {after_uid + 1}:*")
            if status != "OK":
                raise ImapError("search failed")
            uids = sorted(int(u) for u in _bytes(data[0]).split() if int(u) > after_uid)
            messages = [
                msg
                for uid in uids[:MAX_BATCH]
                if (msg := self._fetch(conn, uid, validity)) is not None
            ]
            return validity, messages
        finally:
            conn.logout()

    def append_draft(self, message: bytes) -> None:
        conn = self._open()
        try:
            status, _ = conn.append(self.drafts_mailbox, r"(\Draft \Seen)", None, message)
            if status != "OK":
                raise ImapError("saving the draft failed")
        finally:
            conn.logout()

    def _open(self) -> ImapConnection:
        conn = self.connect(self.host, self.port)
        status, _ = conn.login(self.username, self.password)
        if status != "OK":
            raise ImapError("login failed")
        return conn

    def _fetch(self, conn: ImapConnection, uid: int, validity: str) -> MailMessage | None:
        status, data = conn.uid("FETCH", str(uid), "(RFC822.SIZE)")
        size = _SIZE.search(_bytes(data[0])) if status == "OK" and data else None
        if size is None or int(size.group(1)) > MAX_MESSAGE_BYTES:
            return None
        status, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if status != "OK":
            return None
        raw = next((part[1] for part in data if isinstance(part, tuple) and len(part) == 2), None)
        if not isinstance(raw, bytes):
            return None
        return parse_message(raw, mail_id=f"{self.mailbox}:{validity}:{uid}", uid=uid)


def _bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, tuple) and value and isinstance(value[0], bytes):
        return value[0]
    return b""


def _first(values: list[bytes | None]) -> str | None:
    for value in values:
        if value:
            return value.decode()
    return None
