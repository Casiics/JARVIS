"""Typed records of the private domain. Everything here is classified PRIVATE."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from jarvis_core.contracts.classification import DataClassification

PRIVATE: Literal[DataClassification.PRIVATE] = DataClassification.PRIVATE


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    classification: Literal[DataClassification.PRIVATE] = PRIVATE


class CalendarEvent(_Record):
    uid: str = Field(min_length=1, max_length=500)
    summary: str = Field(default="", max_length=500)
    start: AwareDatetime | date
    end: AwareDatetime | date | None = None
    location: str = Field(default="", max_length=500)
    description: str = Field(default="", max_length=4000)
    recurring: bool = False

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)


class MailMessage(_Record):
    """A message read from IMAP. Its text is external content: only quarantined workers see it."""

    mail_id: str = Field(min_length=1, max_length=200, description="mailbox:uidvalidity:uid")
    uid: int = Field(ge=1)
    message_id: str = Field(default="", max_length=500)
    sender: str = Field(default="", max_length=500)
    to: tuple[str, ...] = ()
    subject: str = Field(default="", max_length=500)
    received_at: AwareDatetime | None = None
    body: str = Field(default="", max_length=20_000)
    truncated: bool = False


class MailCategory(StrEnum):
    ACTION_NEEDED = "action_needed"
    REPLY_SUGGESTED = "reply_suggested"
    FYI = "fyi"
    NEWSLETTER = "newsletter"
    SPAM_OR_PHISHING = "spam_or_phishing"


class TriageDraft(BaseModel):
    """What the quarantined worker returns for one mail."""

    model_config = ConfigDict(extra="forbid")

    category: MailCategory
    summary: str = Field(min_length=1, max_length=400)
    deadline: date | None = None
    suspicious: bool = False


class MailTriage(_Record):
    mail_id: str
    category: MailCategory
    summary: str
    deadline: date | None = None
    suspicious: bool = False
    injection_flags: tuple[str, ...] = ()


class BriefingItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["event", "mail", "reminder"]
    ref: str = Field(min_length=1, max_length=500, description="event uid or mail_id")
    text: str = Field(min_length=1, max_length=300)


class BriefingDraft(BaseModel):
    """What the briefing worker returns: a short overview plus items that cite their source."""

    model_config = ConfigDict(extra="forbid")

    headline: str = Field(min_length=1, max_length=300)
    items: list[BriefingItem] = Field(default_factory=list, max_length=40)


class Briefing(_Record):
    briefing_id: UUID = Field(default_factory=uuid4)
    day: date
    headline: str
    items: tuple[BriefingItem, ...]
    created_at: AwareDatetime
    iterations: int = Field(ge=1)


class ReplyDraftDraft(BaseModel):
    """What the reply worker returns. Subject and recipients are set by code, not the model."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=8000)


class DraftStatus(StrEnum):
    PROPOSED = "proposed"
    SAVED = "saved"
    REJECTED = "rejected"


class MailDraft(_Record):
    draft_id: UUID = Field(default_factory=uuid4)
    in_reply_to: str
    to: tuple[str, ...]
    subject: str = Field(max_length=500)
    body: str = Field(max_length=8000)
    created_at: AwareDatetime
    status: DraftStatus = DraftStatus.PROPOSED


class InboxEntry(_Record):
    """A LifeTipProposed that crossed into the private domain. Proposals only."""

    entry_id: UUID
    summary: str = Field(max_length=2000)
    source_ref: str = Field(max_length=500)
    received_at: AwareDatetime
    dismissed: bool = False
