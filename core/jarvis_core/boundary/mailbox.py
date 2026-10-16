"""File mailboxes for boundary events between processes on one host.

Each direction has its own directory, `<root>/<origin>-to-<target>/`. A domain writes only
into its own outgoing directories and reads only its incoming ones; on a server, directory
ownership enforces that (the sender's user owns the directory, the receiver may read and
delete). The receiver takes the origin from the directory, never from the payload, and runs
every event through the boundary policy again before acting on it.

Files are written to a temporary name and renamed, so a reader never sees half an event.
Anything a reader cannot accept is moved to `rejected/` in that directory and never retried.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import Field, TypeAdapter, ValidationError

from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.events import (
    BoundaryEvent,
    LifeTipProposed,
    ReelSubmitted,
    TaskMetadata,
    TradingIdeaProposed,
)
from jarvis_core.policy.flows import check_flow

AnyEvent = Annotated[
    ReelSubmitted | TaskMetadata | TradingIdeaProposed | LifeTipProposed,
    Field(discriminator="kind"),
]
_EVENT: TypeAdapter[AnyEvent] = TypeAdapter(AnyEvent)
MAX_EVENT_BYTES = 64 * 1024


class MailboxError(Exception):
    pass


@dataclass(frozen=True)
class Letter:
    path: Path
    event: BoundaryEvent


class Mailbox:
    def __init__(self, root: Path, me: Domain) -> None:
        self.root = root
        self.me = me

    def _dir(self, origin: Domain, target: Domain) -> Path:
        return self.root / f"{origin.value}-to-{target.value}"

    def send(self, event: BoundaryEvent) -> Path:
        decision = check_flow(event, caller_domain=self.me)
        if not decision.allowed or event.target_domain is self.me:
            raise MailboxError(f"refused: {decision.reason}")
        directory = self._dir(self.me, event.target_domain)
        directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        name = f"{event.created_at.strftime('%Y%m%dT%H%M%S%f')}-{event.event_id}.json"
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".")
        try:
            os.fchmod(fd, 0o640)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(event.model_dump_json())
                fh.flush()
                os.fsync(fh.fileno())
            target = directory / name
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return target

    def receive(self, origin: Domain) -> list[Letter]:
        """Events from `origin` addressed to this domain, oldest first. Call ack() once handled."""
        directory = self._dir(origin, self.me)
        if not directory.is_dir():
            return []
        letters: list[Letter] = []
        for path in sorted(directory.glob("*.json")):
            event = self._read(path, origin)
            if event is None:
                self._reject(path)
            else:
                letters.append(Letter(path, event))
        return letters

    def ack(self, letter: Letter) -> None:
        letter.path.unlink(missing_ok=True)

    def _read(self, path: Path, origin: Domain) -> BoundaryEvent | None:
        try:
            if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_EVENT_BYTES:
                return None
            event: BoundaryEvent = _EVENT.validate_json(path.read_bytes())
        except (OSError, ValidationError):
            return None
        if event.target_domain is not self.me:
            return None
        if not check_flow(event, caller_domain=origin).allowed:
            return None
        return event

    def _reject(self, path: Path) -> None:
        rejected = path.parent / "rejected"
        rejected.mkdir(exist_ok=True, mode=0o750)
        with contextlib.suppress(OSError):
            os.replace(path, rejected / path.name)
