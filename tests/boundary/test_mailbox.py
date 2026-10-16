"""File mailboxes: events cross only in allowed directions, and the directory names the sender."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from jarvis_core.boundary.mailbox import Mailbox, MailboxError
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.contracts.events import ReelSubmitted, TaskMetadata, TradingIdeaProposed

REEL = "https://www.instagram.com/reel/ABCDE12345/"


def reel(target: Domain = Domain.CONTENT) -> ReelSubmitted:
    return ReelSubmitted(
        origin_domain=Domain.CORE,
        target_domain=target,
        classification=DataClassification.INTERNAL,
        task_id=uuid4(),
        source_ref=REEL,
    )


def status(origin: Domain = Domain.CONTENT, state: str = "running") -> TaskMetadata:
    return TaskMetadata(
        origin_domain=origin,
        target_domain=Domain.CORE,
        classification=DataClassification.INTERNAL,
        task_id=uuid4(),
        state=state,
        duration_ms=0,
    )


def test_core_sends_a_reel_and_content_receives_it_once(tmp_path: Path) -> None:
    core, content = Mailbox(tmp_path, Domain.CORE), Mailbox(tmp_path, Domain.CONTENT)
    event = reel()
    path = core.send(event)

    assert path.parent == tmp_path / "core-to-content"
    assert (path.stat().st_mode & 0o777) == 0o640
    [letter] = content.receive(Domain.CORE)
    assert letter.event == event
    content.ack(letter)
    assert content.receive(Domain.CORE) == []


def test_status_flows_back_in_order(tmp_path: Path) -> None:
    core, content = Mailbox(tmp_path, Domain.CORE), Mailbox(tmp_path, Domain.CONTENT)
    first, second = status(state="running"), status(state="done")
    content.send(first)
    content.send(second)
    assert [letter.event for letter in core.receive(Domain.CONTENT)] == [first, second]


@pytest.mark.parametrize(
    ("me", "event"),
    [
        (Domain.CONTENT, reel()),  # content cannot pretend to be core
        (Domain.CORE, reel(Domain.TRADING)),  # core sends reels to content only
        (Domain.CORE, status(Domain.CORE)),  # nobody writes into its own inbox
        (
            Domain.PRIVATE,
            status(Domain.PRIVATE).model_copy(update={"target_domain": Domain.CONTENT}),
        ),
    ],
)
def test_disallowed_flows_are_never_written(tmp_path: Path, me: Domain, event: object) -> None:
    with pytest.raises(MailboxError):
        Mailbox(tmp_path, me).send(event)  # type: ignore[arg-type]
    assert not any(tmp_path.rglob("*.json"))


def _plant(tmp_path: Path, direction: str, name: str, data: str) -> Path:
    directory = tmp_path / direction
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(data)
    return path


def test_the_directory_names_the_sender_not_the_payload(tmp_path: Path) -> None:
    # Content plants an event claiming to come from core into its own outgoing directory.
    forged = reel().model_copy(update={"target_domain": Domain.CORE})
    _plant(tmp_path, "content-to-core", "a.json", forged.model_dump_json())
    core = Mailbox(tmp_path, Domain.CORE)

    assert core.receive(Domain.CONTENT) == []
    assert (tmp_path / "content-to-core/rejected/a.json").exists()


@pytest.mark.parametrize(
    "data",
    [
        "not json",
        '{"kind": "something_else"}',
        status().model_dump_json().replace('"running"', '"' + "x" * 41 + '"'),
        "x" * 70_000,
    ],
)
def test_garbage_is_set_aside(tmp_path: Path, data: str) -> None:
    _plant(tmp_path, "content-to-core", "b.json", data)
    assert Mailbox(tmp_path, Domain.CORE).receive(Domain.CONTENT) == []
    assert (tmp_path / "content-to-core/rejected/b.json").exists()


def test_events_for_someone_else_are_set_aside(tmp_path: Path) -> None:
    idea = TradingIdeaProposed(
        origin_domain=Domain.CONTENT,
        target_domain=Domain.TRADING,
        classification=DataClassification.INTERNAL,
        summary="x",
        source_ref="ref",
    )
    _plant(tmp_path, "content-to-core", "c.json", idea.model_dump_json())
    assert Mailbox(tmp_path, Domain.CORE).receive(Domain.CONTENT) == []


def test_symlinks_are_not_followed(tmp_path: Path) -> None:
    secret = tmp_path / "secret.json"
    secret.write_text(status().model_dump_json())
    (tmp_path / "content-to-core").mkdir()
    os.symlink(secret, tmp_path / "content-to-core/d.json")
    assert Mailbox(tmp_path, Domain.CORE).receive(Domain.CONTENT) == []
    assert secret.exists()
