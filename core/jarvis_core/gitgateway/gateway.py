"""Apply an agent's patch in a fresh clone and propose it as an auto-improve PR."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from jarvis_core.contracts.audit import AuditAction, AuditDecision, AuditLogEntry
from jarvis_core.contracts.classification import Domain
from jarvis_core.gitgateway.rules import (
    FileChange,
    Violation,
    branch_name,
    check_changes,
    parse_raw_diff,
    validate_branch,
)

AuditSink = Callable[[AuditLogEntry], object]

MAX_PATCH_BYTES = 512 * 1024
ACTOR = "git-gateway"
COMMITTER = ("jarvis-git-gateway", "git-gateway@jarvis.invalid")


class Proposal(BaseModel):
    """A change an agent wants to land. The patch is a unified diff (`git diff` output)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(max_length=48)
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(default="", max_length=10_000)
    patch: str = Field(min_length=1, max_length=MAX_PATCH_BYTES)
    task_id: UUID
    trace_id: str = Field(max_length=64)


@dataclass(frozen=True)
class ProposalResult:
    branch: str
    commit: str
    pull_request: str
    files: tuple[str, ...]


class ProposalRejected(Exception):
    def __init__(self, violations: list[Violation]) -> None:
        self.violations = violations
        summary = ", ".join(f"{v.reason.value}:{v.path}" for v in violations)
        super().__init__(f"proposal rejected ({summary})")


class GitGatewayError(Exception):
    pass


class PullRequestOpener(Protocol):
    def open(self, *, token: str, head: str, base: str, title: str, body: str) -> str: ...


def _git_binary() -> str:
    git = shutil.which("git")
    if git is None:
        raise GitGatewayError("git is not installed")
    return git


@dataclass
class GitGateway:
    """Pushes only `auto-improve/*` branches and never touches protected paths.

    `remote` is the repository URL (https://github.com/<owner>/<repo>.git in production, a
    local path in tests). `token` returns a fresh installation token, or None for remotes that
    need none.
    """

    remote: str
    pulls: PullRequestOpener
    audit: AuditSink
    token: Callable[[], str | None]
    base: str = "main"
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    _git: str = field(default_factory=_git_binary, init=False, repr=False)

    def propose(self, proposal: Proposal) -> ProposalResult:
        branch = branch_name(proposal.topic, self.clock())
        patch = proposal.patch.encode()
        patch_hash = hashlib.sha256(patch).hexdigest()
        token = self.token()
        with tempfile.TemporaryDirectory(prefix="jarvis-gitgw-") as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            work = Path(tmp) / "work"
            env = self._env(home, token)
            self._run(
                ["clone", "--quiet", "--depth", "1", "--single-branch", "--no-tags",
                 "--branch", self.base, self.remote, str(work)],
                env=env, cwd=Path(tmp),
            )  # fmt: skip
            try:
                self._run(["apply", "--index", "--check", "-"], env=env, cwd=work, stdin=patch)
            except GitGatewayError:
                self._audit(proposal, branch, AuditDecision.DENY, "does_not_apply", patch_hash)
                raise
            self._run(["apply", "--index", "-"], env=env, cwd=work, stdin=patch)
            changes = self.staged_changes(work, env)
            violations = check_changes(changes)
            if violations:
                reasons = ",".join(sorted({v.reason.value for v in violations}))
                self._audit(proposal, branch, AuditDecision.DENY, reasons[:100], patch_hash)
                raise ProposalRejected(violations)
            message = f"{proposal.title}\n\n{proposal.body}\n\nJarvis-Task: {proposal.task_id}\n"
            self._run(
                ["-c", f"user.name={COMMITTER[0]}", "-c", f"user.email={COMMITTER[1]}",
                 "commit", "--quiet", "--no-verify", "-F", "-"],
                env=env, cwd=work, stdin=message.encode(),
            )  # fmt: skip
            commit = self._run(["rev-parse", "HEAD"], env=env, cwd=work).decode().strip()
            self._run(
                ["push", "--quiet", "--no-verify", self.remote,
                 f"HEAD:refs/heads/{validate_branch(branch)}"],
                env=env, cwd=work,
            )  # fmt: skip
        url = self.pulls.open(
            token=token or "", head=branch, base=self.base, title=proposal.title, body=proposal.body
        )
        self._audit(proposal, branch, AuditDecision.ALLOW, None, patch_hash, ref=url)
        return ProposalResult(branch, commit, url, tuple(c.path for c in changes))

    def staged_changes(self, work: Path, env: dict[str, str]) -> list[FileChange]:
        raw = self._run(
            ["diff", "--cached", "--raw", "-z", "--no-renames", "--no-abbrev"], env=env, cwd=work
        )
        return parse_raw_diff(raw)

    def _env(self, home: Path, token: str | None) -> dict[str, str]:
        config = {
            "core.hooksPath": os.devnull,
            "core.symlinks": "false",
            "core.fsmonitor": "false",
            "commit.gpgsign": "false",
            "protocol.ext.allow": "never",
            "credential.helper": "",
        }
        if token is not None:
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            config["http.https://github.com/.extraheader"] = f"AUTHORIZATION: basic {basic}"
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(home),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": str(len(config)),
        }
        for i, (key, value) in enumerate(config.items()):
            env[f"GIT_CONFIG_KEY_{i}"] = key
            env[f"GIT_CONFIG_VALUE_{i}"] = value
        return env

    def _run(
        self, args: list[str], *, env: dict[str, str], cwd: Path, stdin: bytes | None = None
    ) -> bytes:
        proc = subprocess.run(  # noqa: S603 - argv list, fixed git binary
            [self._git, *args],
            input=stdin,
            env=env,
            cwd=cwd,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            # stderr may quote patch content; only its hash leaves this function.
            digest = hashlib.sha256(proc.stderr).hexdigest()[:16]
            raise GitGatewayError(f"git {args[0]} failed ({proc.returncode}, stderr {digest})")
        return proc.stdout

    def _audit(
        self,
        proposal: Proposal,
        branch: str,
        decision: AuditDecision,
        policy_id: str | None,
        patch_hash: str,
        ref: str | None = None,
    ) -> None:
        self.audit(
            AuditLogEntry(
                trace_id=proposal.trace_id,
                task_id=proposal.task_id,
                domain=Domain.CORE,
                actor=ACTOR,
                action=AuditAction.SELF_MODIFICATION,
                target=branch,
                decision=decision,
                policy_id=policy_id,
                content_hash=patch_hash,
                content_ref=ref,
            )
        )
