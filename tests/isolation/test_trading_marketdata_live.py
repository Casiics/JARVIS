"""The committed market-data workflow in a real n8n-trading behind the trading gateway.

Imports core/n8n-workflows/trading-marketdata.json unchanged and speaks MCP to it from the
trading agents' network, with the committed policies/mcp-tools/trading.json. Whether the
provider answers depends on the host's internet access, so the call only has to reach n8n.
Opt in with JARVIS_TEST_DOCKER=1.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator

import pytest
import yaml

from tests.isolation.test_isolation_live import (
    COMPOSE,
    HERE,
    PYTHON,
    ROOT,
    Stack,
    _secrets,
    _wait,
    gateway_certs,
    workload_certs,
)
from tests.isolation.test_n8n_mcp_live import _result

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("JARVIS_TEST_DOCKER") != "1", reason="JARVIS_TEST_DOCKER=1 not set"
    ),
]

URL = "https://mcp-gateway-trading:8443/mcp/marketdata"
WORKFLOW = ROOT / "core/n8n-workflows/trading-marketdata.json"


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    tmp = tmp_path_factory.mktemp("n8n-trading")
    _secrets(tmp / "secrets")
    workload_certs(tmp / "workload", "trading")
    override = tmp / "override.yml"
    override.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "mcp-gateway-trading": gateway_certs(tmp / "workload", "trading"),
                    "probe-agent-trading": {
                        "image": PYTHON,
                        "command": ["sleep", "infinity"],
                        "networks": ["net-trading"],
                        "volumes": [
                            f"{HERE / 'probe.py'}:/probe.py:ro",
                            f"{tmp / 'workload' / 'agents'}:/certs:ro",
                        ],
                    },
                }
            }
        )
    )
    env = {**os.environ, "JARVIS_SECRETS_DIR": str(tmp / "secrets")}
    stack = Stack(f"jarvis-md-{uuid.uuid4().hex[:8]}", [COMPOSE, override], env)
    workflow_id = json.loads(WORKFLOW.read_text())[0]["id"]
    try:
        for args in (
            ["import:workflow", "--input=/wf.json"],
            ["publish:workflow", f"--id={workflow_id}"],
        ):
            run = stack.compose(
                "run", "--rm", "-v", f"{WORKFLOW}:/wf.json:ro", "--entrypoint", "n8n",
                "n8n-trading", *args,
            )  # fmt: skip
            assert run.returncode == 0, run.stderr
        services = ["n8n-trading", "opa-trading", "mcp-gateway-trading", "egress-trading"]
        up = stack.compose("up", "-d", *services, "probe-agent-trading")
        assert up.returncode == 0, up.stderr

        def ready() -> bool:
            return bool(_call(stack, "ping", {})["status"] == 200)

        _wait(ready, "n8n MCP endpoint", timeout=240)
        yield stack
    finally:
        stack.compose("down", "-v", "--remove-orphans", timeout=300)


def _call(stack: Stack, method: str, params: dict[str, object]) -> dict[str, object]:
    return stack.probe("probe-agent-trading", "mcp", URL, method, json.dumps(params))


def test_the_workflow_offers_exactly_its_tools(stack: Stack) -> None:
    response = _call(stack, "tools/list", {})
    assert response["status"] == 200
    tools = _result(response)["result"]
    assert isinstance(tools, dict)
    assert sorted(t["name"] for t in tools["tools"]) == ["get_bars", "get_daily"]
    assert all("symbol" in json.dumps(t["inputSchema"]) for t in tools["tools"])


def test_get_bars_runs_in_n8n(stack: Stack) -> None:
    response = _call(stack, "tools/call", {"name": "get_bars", "arguments": {"symbol": "SPY"}})
    assert response["status"] == 200
    result = _result(response)["result"]
    assert isinstance(result, dict) and result["content"]
    # The text is the provider's chart JSON, or n8n's error text when the host has no internet
    # access or the provider rate-limits (both seen). mod_trading.marketdata refuses the latter.
    text = result["content"][0]["text"]
    assert isinstance(text, str) and text


def test_other_tools_never_reach_n8n(stack: Stack) -> None:
    response = _call(stack, "tools/call", {"name": "place_order", "arguments": {}})
    assert response["status"] == 403
