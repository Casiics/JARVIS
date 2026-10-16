"""The reel worker's deck mode: reels arrive from core, status goes back, nothing else does."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from jarvis_core.boundary.mailbox import Mailbox
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import ReelSubmitted, TaskMetadata
from jarvis_core.contracts.task_token import verify_token
from mod_content.reel import __main__ as cli

REEL = "https://www.instagram.com/reel/ABCDE12345/"


class StubPipeline:
    def __init__(self) -> None:
        self.runs: list[tuple[str, str]] = []

    def run(self, url: str, token: str) -> object:
        self.runs.append((url, token))
        raise RuntimeError("sandbox engine missing")


def test_serve_runs_each_submitted_reel_and_reports_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary = tmp_path / "boundary"
    core = Mailbox(boundary, Domain.CORE)
    task_id = uuid4()
    core.send(
        ReelSubmitted(
            origin_domain=Domain.CORE,
            target_domain=Domain.CONTENT,
            classification=DataClassification.INTERNAL,
            task_id=task_id,
            source_ref=REEL,
        )
    )
    stub = StubPipeline()
    key = b"k" * 32
    monkeypatch.setattr(cli, "_build", lambda args, data, mailbox=None: (stub, key))

    code = cli.main(
        [
            "--data-dir",
            str(tmp_path / "content"),
            "serve",
            "--once",
            "--boundary-dir",
            str(boundary),
        ]
    )

    assert code == 0
    [(url, token)] = stub.runs
    assert url == REEL
    claims = verify_token(key, token)
    assert claims.task_id == task_id and claims.domain is Domain.CONTENT
    reports = [letter.event for letter in core.receive(Domain.CONTENT)]
    assert all(isinstance(r, TaskMetadata) and r.task_id == task_id for r in reports)
    assert [(r.state, r.error_class) for r in reports] == [  # type: ignore[attr-defined]
        ("running", None),
        ("escalated", "internal_error"),
    ]
    # Handled once: the event is gone from the inbox.
    assert Mailbox(boundary, Domain.CONTENT).receive(Domain.CORE) == []
