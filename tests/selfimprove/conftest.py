"""A copy of this repository behind a git gateway, as a self-improvement agent would see it."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jarvis_core.contracts.audit import AuditLogEntry
from jarvis_core.gitgateway.gateway import GitGateway

ROOT = Path(__file__).parents[2]
GIT = shutil.which("git") or "git"
NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)


def git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "agent",
        "GIT_AUTHOR_EMAIL": "agent@example.invalid",
        "GIT_COMMITTER_NAME": "agent",
        "GIT_COMMITTER_EMAIL": "agent@example.invalid",
    }
    proc = subprocess.run(  # noqa: S603
        [GIT, *args], cwd=cwd, env=env, capture_output=True, check=True
    )
    return proc.stdout.decode()


@dataclass
class World:
    remote: Path
    agent: Path
    gateway: GitGateway
    audit: list[AuditLogEntry]
    opened: list[str]

    def patch(self, edit: str) -> str:
        git(self.agent, "reset", "--hard", "-q", "origin/main")
        git(self.agent, "clean", "-fdq")
        exec(edit, {"root": self.agent, "Path": Path, "os": os, "shutil": shutil})  # noqa: S102
        git(self.agent, "add", "-A")
        diff = git(self.agent, "diff", "--cached", "--binary")
        git(self.agent, "reset", "-q")
        return diff

    def branches(self) -> list[str]:
        out = git(self.remote, "for-each-ref", "--format=%(refname:short)", "refs/heads")
        return sorted(out.split())


class Pulls:
    def __init__(self, opened: list[str]) -> None:
        self.opened = opened

    def open(self, *, token: str, head: str, base: str, title: str, body: str) -> str:
        self.opened.append(head)
        return f"https://github.invalid/pull/{len(self.opened)}"


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A bare repository holding this checkout's current files (tracked and new)."""
    base = tmp_path_factory.mktemp("exit")
    remote = base / "remote.git"
    git(base, "init", "-q", "--bare", "-b", "main", str(remote))
    seed = base / "seed"
    git(base, "clone", "-q", str(remote), str(seed))
    listed = git(ROOT, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    for name in filter(None, listed.split("\0")):
        source = ROOT / name
        if not source.is_file() or source.is_symlink():
            continue
        target = seed / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "snapshot")
    git(seed, "push", "-q", "origin", "HEAD:main")
    return remote


@pytest.fixture
def world(snapshot: Path, tmp_path: Path) -> World:
    remote = tmp_path / "remote.git"
    shutil.copytree(snapshot, remote)
    agent = tmp_path / "agent"
    git(tmp_path, "clone", "-q", str(remote), str(agent))
    audit: list[AuditLogEntry] = []
    opened: list[str] = []
    gateway = GitGateway(
        remote=f"file://{remote}",
        pulls=Pulls(opened),
        audit=audit.append,
        token=lambda: None,
        clock=lambda: NOW,
    )
    return World(remote, agent, gateway, audit, opened)
