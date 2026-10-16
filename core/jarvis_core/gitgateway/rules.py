"""What the git gateway accepts: branch names, paths and file modes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

# CLAUDE.md, rule 8 (tests/gitgateway/test_rules.py keeps both lists in sync). Entries ending
# in "/" are directories; the rest are single files.
PROTECTED_PATHS: tuple[str, ...] = (
    "core/jarvis_core/policy/",
    "core/jarvis_core/gate/",
    "core/jarvis_core/router/policy.py",
    "core/jarvis_core/audit/",
    "modules/mod-trading/risk/",
    "policies/",
    "docker/",
    "secrets/",
    ".github/",
    ".claude/",
    "CLAUDE.md",
)

# Also refused, because they would change how the gateway itself or git behaves: the gateway's
# own code, submodule definitions and attribute-driven filters.
GATEWAY_GUARDED_PATHS: tuple[str, ...] = (
    "core/jarvis_core/gitgateway/",
    ".gitmodules",
    ".gitattributes",
)

BRANCH_PREFIX = "auto-improve/"
BRANCH_PATTERN = re.compile(r"^auto-improve/\d{8}T\d{6}Z-[a-z0-9]+(?:-[a-z0-9]+)*$")
TOPIC_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_TOPIC = 48
MAX_FILES = 50

REGULAR_FILE = "100644"
EXECUTABLE_FILE = "100755"
ABSENT = "000000"
ALLOWED_MODES = frozenset({REGULAR_FILE, EXECUTABLE_FILE})

_UNSAFE_PATH = re.compile(r"[\x00-\x1f\x7f\\]")


class Reason(StrEnum):
    PROTECTED_PATH = "protected_path"
    GUARDED_PATH = "guarded_path"
    SYMLINK = "symlink"
    SUBMODULE = "submodule"
    MODE_CHANGE = "mode_change"
    UNSUPPORTED_MODE = "unsupported_mode"
    UNSAFE_PATH = "unsafe_path"
    TOO_MANY_FILES = "too_many_files"
    EMPTY = "empty"


@dataclass(frozen=True)
class FileChange:
    """One entry of `git diff --raw --no-renames`: a rename shows up as delete plus add."""

    status: str
    old_mode: str
    new_mode: str
    path: str


@dataclass(frozen=True)
class Violation:
    reason: Reason
    path: str


def _under(path: str, entries: tuple[str, ...]) -> bool:
    # Case-folded, so "Docker/x" or "claude.md" cannot slip past on a case-insensitive checkout.
    folded = path.casefold()
    for entry in entries:
        target = entry.casefold()
        if target.endswith("/"):
            if folded.startswith(target) or folded == target.rstrip("/"):
                return True
        elif folded == target:
            return True
    return False


def is_protected(path: str) -> bool:
    return _under(path, PROTECTED_PATHS)


def check_changes(changes: list[FileChange]) -> list[Violation]:
    if not changes:
        return [Violation(Reason.EMPTY, "")]
    violations: list[Violation] = []
    if len(changes) > MAX_FILES:
        violations.append(Violation(Reason.TOO_MANY_FILES, str(len(changes))))
    for change in changes:
        path = change.path
        if _UNSAFE_PATH.search(path) or path.startswith("/") or ".." in path.split("/"):
            violations.append(Violation(Reason.UNSAFE_PATH, path))
            continue
        if is_protected(path):
            violations.append(Violation(Reason.PROTECTED_PATH, path))
        elif _under(path, GATEWAY_GUARDED_PATHS):
            violations.append(Violation(Reason.GUARDED_PATH, path))
        for mode in (change.old_mode, change.new_mode):
            if mode == "120000":
                violations.append(Violation(Reason.SYMLINK, path))
            elif mode == "160000":
                violations.append(Violation(Reason.SUBMODULE, path))
            elif mode not in ALLOWED_MODES and mode != ABSENT:
                violations.append(Violation(Reason.UNSUPPORTED_MODE, path))
        if ABSENT not in (change.old_mode, change.new_mode) and change.old_mode != change.new_mode:
            violations.append(Violation(Reason.MODE_CHANGE, path))
    return list(dict.fromkeys(violations))


def parse_raw_diff(output: bytes) -> list[FileChange]:
    """Parse `git diff --raw -z --no-renames --no-abbrev` output."""
    fields = output.split(b"\0")
    changes: list[FileChange] = []
    i = 0
    while i + 1 < len(fields) and fields[i]:
        meta = fields[i].decode("ascii").lstrip(":").split()
        if len(meta) != 5:
            raise ValueError("unexpected git diff --raw output")
        old_mode, new_mode, _, _, status = meta
        path = fields[i + 1].decode("utf-8", errors="surrogateescape")
        changes.append(FileChange(status=status, old_mode=old_mode, new_mode=new_mode, path=path))
        i += 2
    return changes


def validate_topic(topic: str) -> str:
    if len(topic) > MAX_TOPIC or not TOPIC_PATTERN.fullmatch(topic):
        raise ValueError("topic must be lowercase words joined by '-', at most 48 characters")
    return topic


def branch_name(topic: str, now: datetime) -> str:
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"{BRANCH_PREFIX}{stamp}-{validate_topic(topic)}"
    if not BRANCH_PATTERN.fullmatch(name):
        raise ValueError("invalid branch name")
    return name


def validate_branch(name: str) -> str:
    if not BRANCH_PATTERN.fullmatch(name):
        raise ValueError(f"the git gateway pushes only {BRANCH_PREFIX}<timestamp>-<topic>")
    return name
