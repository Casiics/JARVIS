"""Static checks on policies/: egress allowlists and MCP tool data."""

from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path

import pytest

from mod_content.reel.sandbox import REEL_EGRESS_ALLOWLIST

POLICIES = Path(__file__).parents[2] / "policies"
HOST = re.compile(r"^\.?([a-z0-9-]+\.)+[a-z]{2,}$")


def allowlist(name: str) -> list[str]:
    lines = (POLICIES / "egress" / f"{name}.allow").read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def covers(entries: list[str], host: str) -> bool:
    return any(
        host == entry or (entry.startswith(".") and (host == entry[1:] or host.endswith(entry)))
        for entry in entries
    )


@pytest.mark.parametrize("name", ["core", "trading", "content", "reel"])
def test_allowlist_entries_are_host_names(name: str) -> None:
    for entry in allowlist(name):
        assert HOST.match(entry), f"{name}: {entry!r} is not a plain host name"
        with pytest.raises(ValueError):
            ipaddress.ip_address(entry.lstrip("."))


@pytest.mark.parametrize("name", ["core", "trading", "content", "reel"])
def test_allowlist_has_no_duplicates_or_overlaps(name: str) -> None:
    entries = allowlist(name)
    assert len(entries) == len(set(entries))
    for entry in entries:
        others = [e for e in entries if e != entry and e.startswith(".")]
        assert not covers(others, entry.lstrip(".")), f"{name}: {entry} overlaps (Squid refuses)"


def test_reel_allowlist_covers_the_sandbox_hosts() -> None:
    for host in REEL_EGRESS_ALLOWLIST:
        assert covers(allowlist("reel"), host), host


def test_trading_has_no_egress_before_phase_6() -> None:
    assert allowlist("trading") == []


@pytest.mark.parametrize("domain", ["trading", "content", "private"])
def test_mcp_tool_data_names_its_domain(domain: str) -> None:
    data = json.loads((POLICIES / "mcp-tools" / f"{domain}.json").read_text())
    assert set(data) == {"domain_tools"}
    tools = data["domain_tools"]
    assert tools["domain"] == domain
    assert all(isinstance(t, str) for t in tools["tools"] + tools["endpoints"])
