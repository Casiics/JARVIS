"""JSON-file stores for a single-host deck, so passkeys and approvals survive a restart.

Production moves both into pg-core with the same interfaces. Writes go to a temporary file
that replaces the old one, so a crash never leaves half a file; files are created 0600.
"""

from __future__ import annotations

import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from uuid import UUID

from pydantic import TypeAdapter

from jarvis_core.api.auth import InMemoryCredentialStore, StoredCredential
from jarvis_core.approvals.grant import Decision
from jarvis_core.approvals.store import InMemoryApprovalStore, PendingApproval

_CREDENTIALS = TypeAdapter(list[StoredCredential])
_APPROVALS = TypeAdapter(list[PendingApproval])


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


class FileCredentialStore(InMemoryCredentialStore):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        self._lock = threading.Lock()
        if path.exists():
            for item in _CREDENTIALS.validate_json(path.read_bytes()):
                super().add(item)

    def add(self, credential: StoredCredential) -> None:
        with self._lock:
            super().add(credential)
            self._save()

    def update_sign_count(self, credential_id: bytes, sign_count: int) -> None:
        with self._lock:
            super().update_sign_count(credential_id, sign_count)
            self._save()

    def _save(self) -> None:
        _write_atomic(self._path, _CREDENTIALS.dump_json(list(self._items.values())))


class FileApprovalStore(InMemoryApprovalStore):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        self._lock = threading.Lock()
        if path.exists():
            self._items = {a.approval_id: a for a in _APPROVALS.validate_json(path.read_bytes())}

    def add(self, approval: PendingApproval) -> None:
        with self._lock:
            super().add(approval)
            self._save()

    def decide(
        self, approval_id: UUID, decision: Decision, by: str, grant_token: str, at: datetime
    ) -> PendingApproval:
        with self._lock:
            decided = super().decide(approval_id, decision, by, grant_token, at)
            self._save()
            return decided

    def _save(self) -> None:
        _write_atomic(self._path, _APPROVALS.dump_json(list(self._items.values())))
