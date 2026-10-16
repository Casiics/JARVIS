"""Each domain package imports only jarvis_core, itself, the stdlib and pydantic.

The trading risk engine is stricter: it may not import jarvis_core or the trading module, so it
can be reviewed as a closed unit.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

MODULES = Path(__file__).parents[2] / "modules"
PACKAGES = {
    "mod_private": (MODULES / "mod-private/mod_private", {"jarvis_core", "mod_private"}),
    "mod_trading": (
        MODULES / "mod-trading/mod_trading",
        {"jarvis_core", "mod_trading", "trading_risk"},
    ),
    "trading_risk": (MODULES / "mod-trading/risk/trading_risk", {"trading_risk"}),
}


def _imports(file: Path) -> set[str]:
    tree = ast.parse(file.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


@pytest.mark.parametrize("package", sorted(PACKAGES))
def test_package_imports_only_what_it_may(package: str) -> None:
    root, allowed = PACKAGES[package]
    files = list(root.rglob("*.py"))
    assert files
    for file in files:
        for name in _imports(file):
            assert (
                name in allowed | {"__future__", "pydantic"} or name in sys.stdlib_module_names
            ), f"{package}/{file.name} imports {name}"


def test_trading_never_reads_secrets_or_calls_models() -> None:
    for root in (PACKAGES["mod_trading"][0], PACKAGES["trading_risk"][0]):
        for file in root.rglob("*.py"):
            text = file.read_text(encoding="utf-8")
            assert "SecretProvider" not in text and "/run/jarvis/secrets" not in text
            assert "jarvis_core.router" not in text, f"{file.name} reaches the model router"
