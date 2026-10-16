"""The git gateway against local repositories: what lands, what is refused, what is audited."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from jarvis_core.contracts.audit import AuditAction, AuditDecision, AuditLogEntry
from jarvis_core.gitgateway.gateway import (
    GitGateway,
    GitGatewayError,
    Proposal,
    ProposalRejected,
)
from jarvis_core.gitgateway.rules import Reason

GIT = shutil.which("git") or "git"
NOW = datetime(2026, 10, 16, 8, 30, 5, tzinfo=UTC)
BRANCH = "auto-improve/20261016T083005Z-better-docs"


def git(cwd: Path, *args: str, stdin: bytes | None = None) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "owner",
        "GIT_AUTHOR_EMAIL": "owner@example.invalid",
        "GIT_COMMITTER_NAME": "owner",
        "GIT_COMMITTER_EMAIL": "owner@example.invalid",
    }
    proc = subprocess.run(  # noqa: S603
        [GIT, *args], cwd=cwd, env=env, input=stdin, capture_output=True, check=True
    )
    return proc.stdout.decode()


@dataclass
class FakePulls:
    opened: list[dict[str, str]] = field(default_factory=list)

    def open(self, *, token: str, head: str, base: str, title: str, body: str) -> str:
        self.opened.append({"token": token, "head": head, "base": base, "title": title})
        return f"https://github.com/o/r/pull/{len(self.opened)}"


@dataclass
class Repo:
    remote: Path
    author: Path

    def diff(self, edit: str) -> str:
        """Run shell-free edits in the author clone and return the patch."""
        git(self.author, "reset", "--hard", "-q", "origin/main")
        git(self.author, "clean", "-fdq")
        exec(edit, {"root": self.author, "Path": Path, "os": os})  # noqa: S102 - test helper
        git(self.author, "add", "-A")
        patch = git(self.author, "diff", "--cached", "--binary")
        git(self.author, "reset", "-q")
        return patch

    def branches(self) -> list[str]:
        out = git(self.remote, "for-each-ref", "--format=%(refname:short)", "refs/heads")
        return sorted(out.split())


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(remote), str(seed))
    (seed / "README.md").write_text("hello\n")
    (seed / "docker").mkdir()
    (seed / "docker/docker-compose.yml").write_text("services: {}\n")
    (seed / "CLAUDE.md").write_text("rules\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "seed")
    git(seed, "push", "-q", "origin", "HEAD:main")
    author = tmp_path / "author"
    git(tmp_path, "clone", "-q", str(remote), str(author))
    return Repo(remote, author)


@dataclass
class Setup:
    gateway: GitGateway
    pulls: FakePulls
    audit: list[AuditLogEntry]


@pytest.fixture
def setup(repo: Repo) -> Setup:
    pulls = FakePulls()
    audit: list[AuditLogEntry] = []
    gateway = GitGateway(
        remote=f"file://{repo.remote}",
        pulls=pulls,
        audit=audit.append,
        token=lambda: None,
        clock=lambda: NOW,
    )
    return Setup(gateway, pulls, audit)


def proposal(patch: str, topic: str = "better-docs") -> Proposal:
    return Proposal(
        topic=topic, title="Better docs", body="why", patch=patch, task_id=uuid4(), trace_id="t"
    )


def test_allowed_change_lands_on_an_auto_improve_branch(repo: Repo, setup: Setup) -> None:
    patch = repo.diff("(root / 'README.md').write_text('hello world\\n')")
    result = setup.gateway.propose(proposal(patch))

    assert result.branch == BRANCH
    assert result.files == ("README.md",)
    assert repo.branches() == [BRANCH, "main"]
    assert git(repo.remote, "show", f"{BRANCH}:README.md") == "hello world\n"
    assert git(repo.remote, "show", "main:README.md") == "hello\n"
    assert git(repo.remote, "log", "-1", "--format=%an", BRANCH).strip() == "jarvis-git-gateway"
    assert "Jarvis-Task: " in git(repo.remote, "log", "-1", "--format=%B", BRANCH)
    assert setup.pulls.opened == [
        {"token": "", "head": BRANCH, "base": "main", "title": "Better docs"}
    ]
    [entry] = setup.audit
    assert entry.action is AuditAction.SELF_MODIFICATION
    assert entry.decision is AuditDecision.ALLOW
    assert entry.target == BRANCH
    assert entry.content_ref == result.pull_request
    assert entry.content_hash is not None


@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        ("(root / 'docker/docker-compose.yml').write_text('x\\n')", Reason.PROTECTED_PATH),
        ("(root / 'CLAUDE.md').unlink()", Reason.PROTECTED_PATH),
        ("os.rename(root / 'README.md', root / 'docker/README.md')", Reason.PROTECTED_PATH),
        (
            "(root / 'policies').mkdir(); (root / 'policies/x.rego').write_text('x')",
            Reason.PROTECTED_PATH,
        ),
        ("(root / '.gitattributes').write_text('* filter=x\\n')", Reason.GUARDED_PATH),
        ("os.symlink('/etc/passwd', root / 'link')", Reason.SYMLINK),
        ("os.chmod(root / 'README.md', 0o755)", Reason.MODE_CHANGE),
    ],
)
def test_refused_changes_push_nothing(repo: Repo, setup: Setup, edit: str, reason: Reason) -> None:
    patch = repo.diff(edit)
    with pytest.raises(ProposalRejected) as exc:
        setup.gateway.propose(proposal(patch))

    assert reason in {v.reason for v in exc.value.violations}
    assert repo.branches() == ["main"]
    assert setup.pulls.opened == []
    [entry] = setup.audit
    assert entry.decision is AuditDecision.DENY
    assert entry.policy_id is not None and reason.value in entry.policy_id


def test_protected_change_hidden_among_allowed_ones_is_refused(repo: Repo, setup: Setup) -> None:
    patch = repo.diff(
        "(root / 'README.md').write_text('x\\n');"
        "(root / 'docker/docker-compose.yml').write_text('y\\n')"
    )
    with pytest.raises(ProposalRejected) as exc:
        setup.gateway.propose(proposal(patch))
    assert [v.path for v in exc.value.violations] == ["docker/docker-compose.yml"]
    assert repo.branches() == ["main"]


def test_patch_that_does_not_apply_is_refused(repo: Repo, setup: Setup) -> None:
    patch = repo.diff("(root / 'README.md').write_text('a\\n')").replace("-hello", "-goodbye")
    with pytest.raises(GitGatewayError):
        setup.gateway.propose(proposal(patch))
    assert repo.branches() == ["main"]
    assert setup.audit[0].decision is AuditDecision.DENY


def test_patch_cannot_write_outside_the_clone(repo: Repo, setup: Setup, tmp_path: Path) -> None:
    patch = (
        "diff --git a/../escape.txt b/../escape.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/../escape.txt\n"
        "@@ -0,0 +1 @@\n"
        "+owned\n"
    )
    with pytest.raises(GitGatewayError):
        setup.gateway.propose(proposal(patch))
    assert not list(tmp_path.rglob("escape.txt"))
    assert repo.branches() == ["main"]


def test_existing_branch_is_never_overwritten(repo: Repo, setup: Setup) -> None:
    setup.gateway.propose(proposal(repo.diff("(root / 'README.md').write_text('one\\n')")))
    with pytest.raises(GitGatewayError):
        setup.gateway.propose(proposal(repo.diff("(root / 'README.md').write_text('two\\n')")))
    assert git(repo.remote, "show", f"{BRANCH}:README.md") == "one\n"


def test_token_travels_only_as_a_github_http_header(setup: Setup, tmp_path: Path) -> None:
    env = setup.gateway._env(tmp_path, "ghs_secret")
    pairs = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    header = pairs["http.https://github.com/.extraheader"]
    basic = base64.b64encode(b"x-access-token:ghs_secret").decode()
    assert header == f"AUTHORIZATION: basic {basic}"
    assert pairs["core.hooksPath"] == os.devnull
    others = [v for k, v in env.items() if not k.startswith("GIT_CONFIG_VALUE")]
    assert all("ghs_secret" not in v for v in others)
    assert "http.https://github.com/.extraheader" not in {
        k for k in setup.gateway._env(tmp_path, None).values()
    }
