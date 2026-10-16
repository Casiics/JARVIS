from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jarvis_core.gitgateway.rules import (
    GATEWAY_GUARDED_PATHS,
    PROTECTED_PATHS,
    FileChange,
    Reason,
    branch_name,
    check_changes,
    is_protected,
    parse_raw_diff,
    validate_branch,
)

ROOT = Path(__file__).parents[2]


def change(path: str, old: str = "100644", new: str = "100644", status: str = "M") -> FileChange:
    return FileChange(status=status, old_mode=old, new_mode=new, path=path)


def test_protected_paths_match_claude_md() -> None:
    text = (ROOT / "CLAUDE.md").read_text()
    rule = next(line for line in text.splitlines() if line.startswith("8. **Protected paths**"))
    listed = re.findall(r"`([^`]+)`", rule.split("are changed only")[0])
    assert tuple(listed) == PROTECTED_PATHS


def test_protected_and_guarded_paths_match_codeowners() -> None:
    lines = (ROOT / ".github/CODEOWNERS").read_text().splitlines()
    owned = [line.split()[0].lstrip("/") for line in lines if line.startswith("/")]
    assert tuple(owned) == PROTECTED_PATHS + GATEWAY_GUARDED_PATHS


@pytest.mark.parametrize(
    "path",
    [
        "docker/docker-compose.yml",
        "policies/mcp/authz.rego",
        "core/jarvis_core/gate/action_gate.py",
        "core/jarvis_core/router/policy.py",
        "CLAUDE.md",
        ".github/workflows/ci.yml",
        "Docker/docker-compose.yml",
        "claude.md",
        "POLICIES/egress/core.allow",
        "docker",
    ],
)
def test_protected_paths_are_refused(path: str) -> None:
    assert is_protected(path)
    assert Reason.PROTECTED_PATH in {v.reason for v in check_changes([change(path)])}


@pytest.mark.parametrize(
    "path",
    [
        "core/jarvis_core/router/gateway.py",
        "core/jarvis_core/policy_notes.md",
        "docs/runbooks/x.md",
        "dockerfile.md",
        "modules/mod-trading/riskless.py",
        "CLAUDE.md.bak",
    ],
)
def test_neighbours_of_protected_paths_are_allowed(path: str) -> None:
    assert not is_protected(path)
    assert check_changes([change(path)]) == []


def test_gateway_guards_its_own_code_and_git_metadata() -> None:
    for path in (
        "core/jarvis_core/gitgateway/rules.py",
        ".gitmodules",
        ".gitattributes",
        "modules/mod-trading/mod_trading/executor.py",
        "Modules/mod-trading/mod_trading/Broker.py",
    ):
        assert [v.reason for v in check_changes([change(path)])] == [Reason.GUARDED_PATH]


def test_rename_into_protected_path_is_caught_as_delete_plus_add() -> None:
    changes = [
        change("docs/x.md", new="000000", status="D"),
        change("docker/x.md", old="000000", status="A"),
    ]
    assert [v.path for v in check_changes(changes)] == ["docker/x.md"]


def test_symlinks_submodules_and_mode_changes_are_refused() -> None:
    assert check_changes([change("a", old="000000", new="120000", status="A")])[0].reason is (
        Reason.SYMLINK
    )
    assert check_changes([change("a", old="000000", new="160000", status="A")])[0].reason is (
        Reason.SUBMODULE
    )
    assert check_changes([change("a", new="100755")])[0].reason is Reason.MODE_CHANGE
    assert check_changes([change("a", old="100644", new="120000", status="T")])[0].reason is (
        Reason.SYMLINK
    )
    assert check_changes([change("new.sh", old="000000", new="100755", status="A")]) == []


def test_unsafe_paths_and_limits() -> None:
    for path in ("../x", "/etc/passwd", "a\\b", "a\nb"):
        assert check_changes([change(path)])[0].reason is Reason.UNSAFE_PATH
    assert check_changes([])[0].reason is Reason.EMPTY
    many = [change(f"docs/{i}.md") for i in range(51)]
    assert check_changes(many)[0].reason is Reason.TOO_MANY_FILES


def test_parse_raw_diff() -> None:
    raw = (
        b":100644 100644 " + b"a" * 40 + b" " + b"b" * 40 + b" M\0docs/a.md\0"
        b":000000 120000 " + b"0" * 40 + b" " + b"c" * 40 + b" A\0link\0"
    )
    assert parse_raw_diff(raw) == [
        FileChange("M", "100644", "100644", "docs/a.md"),
        FileChange("A", "000000", "120000", "link"),
    ]


def test_branch_names() -> None:
    now = datetime(2026, 10, 16, 8, 30, 5, tzinfo=UTC)
    assert branch_name("tighten-gate", now) == "auto-improve/20261016T083005Z-tighten-gate"
    for bad in ("", "Upper", "a/b", "a--b", "-a", "a" * 49, "a b", "x..y"):
        with pytest.raises(ValueError):
            branch_name(bad, now)
    for bad in ("main", "auto-improve/x", "auto-improve/20261016T083005Z-a/../main"):
        with pytest.raises(ValueError):
            validate_branch(bad)
