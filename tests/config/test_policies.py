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


WORKFLOWS = POLICIES.parent / "core" / "n8n-workflows"
URL_HOST = re.compile(r"https://([a-z0-9.-]+)/")


def workflow_nodes(domain: str) -> list[dict[str, object]]:
    nodes: list[dict[str, object]] = []
    for path in sorted(WORKFLOWS.glob(f"{domain}-*.json")):
        for workflow in json.loads(path.read_text()):
            assert workflow["active"] is False, f"{path.name}: committed workflows start inactive"
            nodes.extend(workflow["nodes"])
    return nodes


def test_trading_egress_is_exactly_its_workflow_hosts() -> None:
    hosts = {
        match
        for node in workflow_nodes("trading")
        for match in URL_HOST.findall(json.dumps(node.get("parameters", {})))
    }
    assert hosts, "no trading workflow calls out"
    entries = allowlist("trading")
    for host in hosts:
        assert covers(entries, host), f"{host} is not on trading.allow"
    for entry in entries:
        assert any(covers([entry], host) for host in hosts), f"{entry} is allowed but unused"


@pytest.mark.parametrize("domain", ["trading"])
def test_mcp_tool_data_matches_the_committed_workflows(domain: str) -> None:
    tools = json.loads((POLICIES / "mcp-tools" / f"{domain}.json").read_text())["domain_tools"]
    nodes = workflow_nodes(domain)
    triggers = [n for n in nodes if n["type"] == "@n8n/n8n-nodes-langchain.mcpTrigger"]
    assert sorted(tools["endpoints"]) == sorted(n["parameters"]["path"] for n in triggers)  # type: ignore[index]
    assert sorted(tools["tools"]) == sorted(str(n["name"]) for n in nodes if n not in triggers)


@pytest.mark.parametrize("domain", ["trading", "content", "private"])
def test_mcp_tool_data_names_its_domain(domain: str) -> None:
    data = json.loads((POLICIES / "mcp-tools" / f"{domain}.json").read_text())
    assert set(data) == {"domain_tools"}
    tools = data["domain_tools"]
    assert tools["domain"] == domain
    assert all(isinstance(t, str) for t in tools["tools"] + tools["endpoints"])
