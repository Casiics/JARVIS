"""Skill proposals: a verified multi-step solution becomes `skills/<domain>/<name>/SKILL.md`.

A skill only ever lands as a pull request through the git gateway, so it passes the gateway's
path rules, CI and a human merge. This module builds the patch itself, which keeps the change
to exactly one new file under `skills/<domain>/`. It refuses:

- the private domain: its knowledge stays local, and a repository on GitHub is not local;
- skills without evidence, meaning the task ids whose verified runs the skill is drawn from;
- text that looks like a secret or credential, or that addresses protected paths.

Skills drawn from tainted tasks (that saw external content) are allowed but say so in the PR,
because the reviewer should read them as untrusted input.
"""

from __future__ import annotations

import re
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.events import Taint
from jarvis_core.gitgateway.gateway import GitGateway, Proposal, ProposalResult
from jarvis_core.gitgateway.rules import is_protected

NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SECRET_PATTERNS = [
    re.compile(p)
    for p in (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\bsk-[A-Za-z0-9_-]{16,}",
        r"\bgh[pousr]_[A-Za-z0-9]{20,}",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bxox[abpr]-[A-Za-z0-9-]{10,}",
        r"\b[0-9a-f]{40,}\b",
        r"(?i)\b(password|passwd|api[_-]?key|secret|token)\s*[:=]\s*\S{6,}",
        r"/run/jarvis/secrets",
    )
]


class SkillRejected(Exception):
    pass


class SkillProposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    domain: Domain
    name: str = Field(min_length=3, max_length=40)
    description: str = Field(min_length=10, max_length=300)
    body: str = Field(min_length=20, max_length=20_000)
    evidence: tuple[UUID, ...] = Field(min_length=1, max_length=20)
    taint: Taint = Taint.NONE

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not NAME.fullmatch(value):
            raise ValueError("skill names are lowercase words joined by '-'")
        return value

    @field_validator("description")
    @classmethod
    def _one_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("the description is one line")
        return value

    @property
    def path(self) -> str:
        return f"skills/{self.domain.value}/{self.name}/SKILL.md"


def render(skill: SkillProposal) -> str:
    evidence = ", ".join(str(t) for t in skill.evidence)
    return (
        "---\n"
        f"name: {skill.name}\n"
        f"description: {skill.description}\n"
        f"domain: {skill.domain.value}\n"
        f"evidence: [{evidence}]\n"
        f"taint: {skill.taint.value}\n"
        "---\n\n"
        f"{skill.body.strip()}\n"
    )


def looks_like_secret(text: str) -> bool:
    return any(p.search(text) for p in _SECRET_PATTERNS)


def check(skill: SkillProposal) -> None:
    if skill.domain is Domain.PRIVATE:
        raise SkillRejected("private-domain skills stay local; they are never proposed to git")
    text = f"{skill.description}\n{skill.body}"
    if looks_like_secret(text):
        raise SkillRejected("the skill contains something that looks like a secret")
    if any(ch in text for ch in ("\x00", "\x1b")):
        raise SkillRejected("control characters are not allowed")
    for word in re.findall(r"[\w./-]+/[\w./-]*", text):
        if is_protected(word.strip("./")):
            raise SkillRejected(f"skills may not address protected paths ({word})")


def new_file_patch(path: str, content: str) -> str:
    """A `git diff` style patch that adds exactly one regular file."""
    lines = content.splitlines()
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{body}"
    )


def propose_skill(
    gateway: GitGateway, skill: SkillProposal, *, task_id: UUID, trace_id: str | None = None
) -> ProposalResult:
    check(skill)
    note = (
        "\n\n**Drawn from tasks that saw external content.** Read it as untrusted input."
        if skill.taint is not Taint.NONE
        else ""
    )
    proposal = Proposal(
        topic=f"skill-{skill.name}"[:48].rstrip("-"),
        title=f"Skill proposal: {skill.domain.value}/{skill.name}",
        body=(
            f"{skill.description}\n\nEvidence (verified task ids): "
            f"{', '.join(str(t) for t in skill.evidence)}{note}"
        ),
        patch=new_file_patch(skill.path, render(skill)),
        task_id=task_id,
        trace_id=trace_id or uuid4().hex,
    )
    return gateway.propose(proposal)
