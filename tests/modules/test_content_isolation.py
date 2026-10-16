"""The content module imports only jarvis_core, its own package, the stdlib and pydantic."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2] / "modules/mod-content/mod_content"
ALLOWED_THIRD_PARTY = {"pydantic"}


def _imports(file: Path) -> set[str]:
    tree = ast.parse(file.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_content_imports_no_other_domain() -> None:
    files = list(ROOT.rglob("*.py"))
    assert files
    for file in files:
        for name in _imports(file):
            assert (
                name in {"jarvis_core", "mod_content", "__future__"}
                or name in sys.stdlib_module_names
                or name in ALLOWED_THIRD_PARTY
            ), f"{file.name} imports {name}"


def test_content_never_reads_secrets_directly() -> None:
    for file in ROOT.rglob("*.py"):
        text = file.read_text(encoding="utf-8")
        assert "/run/jarvis/secrets" not in text
        assert "SecretProvider" not in text
