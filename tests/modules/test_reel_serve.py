"""The reel worker's deck mode: only tasks core dispatched run, status goes back, nothing else."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jarvis_core.boundary.mailbox import Mailbox
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import ReelSubmitted, Taint, TaskMetadata
from jarvis_core.contracts.task_token import (
    issue_signed_token,
    issue_token,
    public_key_hex,
)
from mod_content.reel import __main__ as cli

REEL = "https://www.instagram.com/reel/ABCDE12345/"
CORE_KEY = Ed25519PrivateKey.from_private_bytes(b"c" * 32)


class StubPipeline:
    def __init__(self) -> None:
        self.runs: list[tuple[str, str]] = []

    def run(self, url: str, token: str) -> object:
        self.runs.append((url, token))
        raise RuntimeError("sandbox engine missing")


def core_token(
    task_id: UUID,
    *,
    signer: Ed25519PrivateKey | None = None,
    domain: Domain = Domain.CONTENT,
    ttl: timedelta = timedelta(hours=2),
) -> str:
    return issue_signed_token(
        signer or CORE_KEY,
        task_id=task_id,
        domain=domain,
        allowed_tools=frozenset({"sandbox.fetch"}),
        budget_eur=0.0,
        taint=Taint.EXTERNAL_CONTENT,
        ttl=ttl,
    )


def submit(boundary: Path, task_id: UUID, token: str | None) -> None:
    Mailbox(boundary, Domain.CORE).send(
        ReelSubmitted(
            origin_domain=Domain.CORE,
            target_domain=Domain.CONTENT,
            classification=DataClassification.INTERNAL,
            task_id=task_id,
            source_ref=REEL,
            task_token=token,
        )
    )


def serve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str) -> tuple[int, StubPipeline]:
    stub = StubPipeline()
    monkeypatch.setattr(cli, "_build", lambda args, data, tokens, mailbox=None: stub)
    code = cli.main(
        [
            "--data-dir",
            str(tmp_path / "content"),
            "serve",
            "--once",
            "--boundary-dir",
            str(tmp_path / "boundary"),
            *extra,
        ]
    )
    return code, stub


def reports(boundary: Path) -> list[tuple[str, str | None]]:
    out = []
    for letter in Mailbox(boundary, Domain.CORE).receive(Domain.CONTENT):
        assert isinstance(letter.event, TaskMetadata)
        out.append((letter.event.state, letter.event.error_class))
    return out


KEY_ARGS = ("--task-public-key", public_key_hex(CORE_KEY))


def test_serve_runs_a_core_dispatched_reel_and_reports_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = uuid4()
    token = core_token(task_id)
    submit(tmp_path / "boundary", task_id, token)

    code, stub = serve(tmp_path, monkeypatch, *KEY_ARGS)

    assert code == 0
    assert stub.runs == [(REEL, token)]
    assert reports(tmp_path / "boundary") == [("running", None), ("escalated", "internal_error")]
    # Handled once: the event is gone from the inbox.
    assert Mailbox(tmp_path / "boundary", Domain.CONTENT).receive(Domain.CORE) == []


@pytest.mark.parametrize(
    ("make_token", "error"),
    [
        (lambda task_id: None, "task_token_missing"),
        # Minted by someone without core's key, e.g. the worker itself.
        (
            lambda task_id: core_token(
                task_id, signer=Ed25519PrivateKey.from_private_bytes(b"w" * 32)
            ),
            "task_token_invalid",
        ),
        # The old self-issued HMAC kind is not accepted any more.
        (
            lambda task_id: issue_token(
                b"k" * 32,
                task_id=task_id,
                domain=Domain.CONTENT,
                allowed_tools=frozenset({"sandbox.fetch"}),
                budget_eur=0.0,
            ),
            "task_token_invalid",
        ),
        (lambda task_id: core_token(task_id, ttl=timedelta(seconds=-1)), "task_token_invalid"),
        (lambda task_id: core_token(uuid4()), "task_token_mismatch"),
        (lambda task_id: core_token(task_id, domain=Domain.TRADING), "task_token_mismatch"),
    ],
    ids=["missing", "forged", "hmac", "expired", "other-task", "other-domain"],
)
def test_serve_refuses_reels_without_a_valid_core_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_token: object,
    error: str,
) -> None:
    task_id = uuid4()
    submit(tmp_path / "boundary", task_id, make_token(task_id))  # type: ignore[operator]

    code, stub = serve(tmp_path, monkeypatch, *KEY_ARGS)

    assert code == 0
    assert stub.runs == []
    assert reports(tmp_path / "boundary") == [("escalated", error)]


def test_serve_does_not_start_without_cores_public_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JARVIS_TASK_PUBLIC_KEY", raising=False)
    with pytest.raises(SystemExit, match="public key"):
        serve(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="32 bytes"):
        serve(tmp_path, monkeypatch, "--task-public-key", "abcd")


def test_ingest_mints_tokens_only_when_told_to(tmp_path: Path) -> None:
    code = cli.main(["--data-dir", str(tmp_path), "ingest", REEL])
    assert code == 2
    assert not (tmp_path / "runs.jsonl").exists()
