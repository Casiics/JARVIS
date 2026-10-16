"""Skill proposals land as one new file under skills/<domain>/, through the git gateway."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from jarvis_core.contracts.audit import AuditDecision
from jarvis_core.contracts.classification import Domain
from jarvis_core.contracts.events import Taint
from jarvis_core.selfimprove.skills import (
    SkillProposal,
    SkillRejected,
    check,
    propose_skill,
    render,
)
from tests.selfimprove.conftest import World, git

BODY = (
    "## Steps\n\n1. Export the reel's transcript.\n2. Summarise it into three bullet points.\n"
    "3. Check every number against the transcript before saving.\n"
)


def skill(**overrides: object) -> SkillProposal:
    base: dict[str, object] = {
        "domain": Domain.CONTENT,
        "name": "reel-summary-check",
        "description": "Summarise a reel and check every number against its transcript",
        "body": BODY,
        "evidence": (uuid4(),),
    }
    base.update(overrides)
    return SkillProposal.model_validate(base)


def test_skill_lands_as_one_new_file(world: World) -> None:
    proposal = skill(taint=Taint.EXTERNAL_CONTENT)
    result = propose_skill(world.gateway, proposal, task_id=uuid4())
    assert result.files == ("skills/content/reel-summary-check/SKILL.md",)
    assert result.branch.startswith("auto-improve/") and "skill-reel-summary-check" in result.branch
    landed = git(world.remote, "show", f"{result.branch}:{proposal.path}")
    assert landed == render(proposal)
    assert "taint: external_content" in landed
    assert world.audit[-1].decision is AuditDecision.ALLOW


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"domain": Domain.PRIVATE}, "stay local"),
        ({"body": BODY + "api_key = abcdef123456"}, "secret"),  # gitleaks:allow (test input)
        ({"body": BODY + "-----BEGIN OPENSSH PRIVATE KEY-----"}, "secret"),
        ({"body": BODY + "Use ghp_abcdefghijklmnopqrstuvwxyz0123"}, "secret"),
        ({"body": BODY + "Then raise limits in modules/mod-trading/risk/limits.json"}, "protected"),
        ({"body": BODY + "Edit policies/egress/content.allow first"}, "protected"),
        ({"body": BODY + "\x1b[31m"}, "control"),
    ],
)
def test_unsafe_skills_are_refused(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(SkillRejected, match=message):
        check(skill(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "../escape"},
        {"name": "Has Spaces"},
        {"evidence": ()},
        {"description": "two\nlines here"},
        {"extra": 1},
    ],
)
def test_malformed_skills_do_not_validate(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        skill(**overrides)


def test_an_existing_skill_is_not_overwritten(world: World, tmp_path: Path) -> None:
    proposal = skill()
    first = propose_skill(world.gateway, proposal, task_id=uuid4())
    git(world.remote, "update-ref", "refs/heads/main", f"refs/heads/{first.branch}")
    from jarvis_core.gitgateway.gateway import GitGatewayError

    with pytest.raises(GitGatewayError):
        propose_skill(world.gateway, skill(evidence=(uuid4(),)), task_id=uuid4())
    assert world.audit[-1].policy_id == "does_not_apply"
