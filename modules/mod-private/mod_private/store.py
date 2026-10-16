"""File store of the private domain, until pg-private takes over with the same interface.

One JSON file per collection under a 0700 directory, each file 0600 and replaced atomically.
The directory lives on the private domain's own volume; nothing in it leaves the domain.
"""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Callable
from datetime import date
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, TypeAdapter

from mod_private.schemas import (
    Briefing,
    CalendarEvent,
    InboxEntry,
    MailDraft,
    MailMessage,
    MailTriage,
)


def write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Collection[T: BaseModel]:
    def __init__(self, path: Path, model: type[T], key: Callable[[T], str]) -> None:
        self._path = path
        self._adapter: TypeAdapter[list[T]] = TypeAdapter(list[model])  # type: ignore[valid-type]
        self._key = key
        self._lock = threading.Lock()
        self._items: dict[str, T] = {}
        if path.exists():
            self._items = {key(i): i for i in self._adapter.validate_json(path.read_bytes())}

    def put(self, item: T) -> None:
        with self._lock:
            self._items[self._key(item)] = item
            write_atomic(self._path, self._adapter.dump_json(list(self._items.values())))

    def put_many(self, items: list[T]) -> None:
        with self._lock:
            for item in items:
                self._items[self._key(item)] = item
            write_atomic(self._path, self._adapter.dump_json(list(self._items.values())))

    def get(self, key: str) -> T | None:
        return self._items.get(key)

    def all(self) -> list[T]:
        return list(self._items.values())


class SyncState(BaseModel):
    uidvalidity: str | None = None
    last_uid: int = 0


class PrivateStore:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        self.root = root
        self.events: Collection[CalendarEvent] = Collection(
            root / "events.json", CalendarEvent, lambda e: e.uid
        )
        self.mails: Collection[MailMessage] = Collection(
            root / "mails.json", MailMessage, lambda m: m.mail_id
        )
        self.triage: Collection[MailTriage] = Collection(
            root / "triage.json", MailTriage, lambda t: t.mail_id
        )
        self.briefings: Collection[Briefing] = Collection(
            root / "briefings.json", Briefing, lambda b: b.day.isoformat()
        )
        self.drafts: Collection[MailDraft] = Collection(
            root / "drafts.json", MailDraft, lambda d: str(d.draft_id)
        )
        self.inbox: Collection[InboxEntry] = Collection(
            root / "inbox.json", InboxEntry, lambda i: str(i.entry_id)
        )
        self._sync_path = root / "sync.json"

    def sync_state(self) -> SyncState:
        if not self._sync_path.exists():
            return SyncState()
        return SyncState.model_validate_json(self._sync_path.read_bytes())

    def save_sync_state(self, state: SyncState) -> None:
        write_atomic(self._sync_path, state.model_dump_json().encode())

    def briefing(self, day: date) -> Briefing | None:
        return self.briefings.get(day.isoformat())

    def draft(self, draft_id: UUID) -> MailDraft | None:
        return self.drafts.get(str(draft_id))

    def untriaged(self) -> list[MailMessage]:
        return [m for m in self.mails.all() if self.triage.get(m.mail_id) is None]
