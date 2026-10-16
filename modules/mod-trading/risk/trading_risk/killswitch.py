"""The kill switch: one file outside every model's reach, failing closed.

The state file holds "released" or an engagement record. Anything else (missing file,
unreadable file, unknown content, a directory that cannot be read) counts as engaged, so a
broken deployment stops trading instead of trading blindly. Engaging is allowed to any
deterministic caller; releasing is a human step (`python -m mod_trading kill-switch release`
on the host), never something an agent can request.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

RELEASED = "released"


class KillSwitchState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    engaged: bool
    reason: str = Field(default="", max_length=200)
    since: AwareDatetime | None = None


class KillSwitch:
    def __init__(self, path: Path) -> None:
        self.path = path

    def state(self) -> KillSwitchState:
        try:
            text = self.path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return KillSwitchState(engaged=True, reason="no kill switch state; fail closed")
        except OSError:
            return KillSwitchState(engaged=True, reason="kill switch unreadable; fail closed")
        if text == RELEASED:
            return KillSwitchState(engaged=False)
        try:
            state = KillSwitchState.model_validate_json(text)
        except ValidationError:
            return KillSwitchState(engaged=True, reason="kill switch state invalid; fail closed")
        if not state.engaged:
            return KillSwitchState(engaged=True, reason="only 'released' releases; fail closed")
        return state

    def engaged(self) -> bool:
        return self.state().engaged

    def engage(self, reason: str, *, now: datetime | None = None) -> None:
        state = KillSwitchState(engaged=True, reason=reason[:200], since=now or datetime.now(UTC))
        self._write(json.dumps(state.model_dump(mode="json")))

    def release(self) -> None:
        self._write(RELEASED)

    def _write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".killswitch-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
