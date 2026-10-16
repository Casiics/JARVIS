"""A real n8n MCP Server Trigger behind the content domain's policy gateway.

Imports a workflow with one tool (calculator) into n8n-content, then speaks MCP to it from the
agents' network: the listed tool works end to end, anything else stops at the gateway.
Opt in with JARVIS_TEST_DOCKER=1.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator

import pytest
import yaml

from tests.isolation.test_isolation_live import COMPOSE, HERE, PYTHON, Stack, _secrets, _wait

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("JARVIS_TEST_DOCKER") != "1", reason="JARVIS_TEST_DOCKER=1 not set"
    ),
]

URL = "http://mcp-gateway-content:8080/mcp/test"


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    tmp = tmp_path_factory.mktemp("n8n")
    _secrets(tmp / "secrets")
    tools = tmp / "tools.json"
    tools.write_text(
        json.dumps(
            {"domain_tools": {"domain": "content", "endpoints": ["test"], "tools": ["calculator"]}}
        )
    )
    tools.chmod(0o644)
    override = tmp / "override.yml"
    override.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "opa-content": {"volumes": [f"{tools}:/policies/data.json:ro"]},
                    "probe-agent-content": {
                        "image": PYTHON,
                        "command": ["sleep", "infinity"],
                        "networks": ["net-content"],
                        "volumes": [f"{HERE / 'probe.py'}:/probe.py:ro"],
                    },
                }
            }
        )
    )
    env = {**os.environ, "JARVIS_SECRETS_DIR": str(tmp / "secrets")}
    stack = Stack(f"jarvis-n8n-{uuid.uuid4().hex[:8]}", [COMPOSE, override], env)
    workflow = HERE / "n8n-mcp-workflow.json"
    try:
        for args in (
            ["import:workflow", "--input=/wf.json"],
            ["publish:workflow", "--id=jarvisMcpTest01"],
        ):
            run = stack.compose(
                "run", "--rm", "-v", f"{workflow}:/wf.json:ro", "--entrypoint", "n8n",
                "n8n-content", *args,
            )  # fmt: skip
            assert run.returncode == 0, run.stderr
        services = ["n8n-content", "opa-content", "mcp-gateway-content", "egress-content"]
        up = stack.compose("up", "-d", *services, "probe-agent-content")
        assert up.returncode == 0, up.stderr

        def ready() -> bool:
            return bool(_call(stack, "ping", {})["status"] == 200)

        _wait(ready, "n8n MCP endpoint", timeout=240)
        yield stack
    finally:
        stack.compose("down", "-v", "--remove-orphans", timeout=300)


def _call(stack: Stack, method: str, params: dict[str, object]) -> dict[str, object]:
    """One MCP session per call: initialize, initialized, then the request."""
    return stack.probe("probe-agent-content", "mcp", URL, method, json.dumps(params))


def _result(response: dict[str, object]) -> dict[str, object]:
    # Streamable HTTP answers as one server-sent event: "event: message\ndata: {...}".
    text = str(response["body"])
    data = next(line for line in text.splitlines() if line.startswith("data: "))
    message: dict[str, object] = json.loads(data.removeprefix("data: "))
    return message


def test_listed_tool_runs_in_n8n(stack: Stack) -> None:
    response = _call(stack, "tools/call", {"name": "calculator", "arguments": {"input": "6*7"}})
    assert response["status"] == 200
    assert _result(response)["result"] == {"content": [{"type": "text", "text": "42"}]}


def test_tools_list_passes(stack: Stack) -> None:
    response = _call(stack, "tools/list", {})
    assert response["status"] == 200
    tools = _result(response)["result"]
    assert isinstance(tools, dict)
    assert [t["name"] for t in tools["tools"]] == ["calculator"]


def test_unlisted_tool_never_reaches_n8n(stack: Stack) -> None:
    response = _call(stack, "tools/call", {"name": "send_email", "arguments": {}})
    assert response["status"] == 403


def test_agents_cannot_reach_n8n_directly(stack: Stack) -> None:
    assert not stack.probe("probe-agent-content", "tcp", "n8n-content", "5678")["ok"]
