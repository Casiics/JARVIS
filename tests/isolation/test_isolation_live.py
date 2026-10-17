"""Live network isolation tests against the real Compose stack (Phase 4 exit criterion).

Starts the egress proxies, the policy gateways and stand-ins for the internet and for an MCP
server in Docker, then probes from the positions agents, n8n and sandboxes occupy. The fake
internet lives on net-egress under real host names (Docker network aliases), pinned to
198.18.0.0/24 so it is not mistaken for an internal address.

Opt in with JARVIS_TEST_DOCKER=1 (needs Docker with the Compose plugin and network access to
pull the pinned images).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from tests.workload_ca import WorkloadCa

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("JARVIS_TEST_DOCKER") != "1", reason="JARVIS_TEST_DOCKER=1 not set"
    ),
]

ROOT = Path(__file__).parents[2]
COMPOSE = ROOT / "docker/docker-compose.yml"
HERE = Path(__file__).parent
PYTHON = "python:3.12.11-slim-bookworm"
CALDAV = "caldav.jarvis.test"
IMAP = "imap.jarvis.test"
PRIVATE_PROXY_IP = "10.213.40.2"

SERVICES = [
    "egress-content",
    "egress-reel",
    "egress-private",
    "opa-content",
    "mcp-gateway-content",
    "opa-trading",
    "mcp-gateway-trading",
    "fake-telegram",
    "fake-other",
    "fake-instagram",
    "fake-caldav",
    "fake-imap",
    "fake-unlisted-private",
    "mcp-fake-content",
    "probe-agent-content",
    "probe-n8n-content",
    "probe-sandbox",
    "probe-n8n-private",
    "probe-agent-private",
]


def _certificate(directory: Path) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "jarvis-test")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=2))
        .sign(key, hashes.SHA256())
    )
    (directory / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    for path in directory.iterdir():
        path.chmod(0o644)


def workload_certs(directory: Path, agent_domain: str) -> WorkloadCa:
    """Test stand-ins for pki-issue's output: one gateway certificate per domain under
    gateways/<domain>, and client certificates for probes under agents/:
    ok (an agent of agent_domain), other-domain, expired and rogue (another CA)."""
    ca = WorkloadCa()
    for domain in ("trading", "content", "private"):
        gateway = directory / "gateways" / domain
        ca.issue(gateway, domain, "mcp-gateway", dns=f"mcp-gateway-{domain}", rotate=True)
    agents = directory / "agents"
    other = "trading" if agent_domain != "trading" else "content"
    ca.issue(agents / "ok", agent_domain, "agent")
    ca.issue(agents / "other-domain", other, "agent")
    ca.issue(agents / "expired", agent_domain, "agent", expired=True)
    WorkloadCa("Rogue").issue(agents / "rogue", agent_domain, "agent")
    for path in [directory, *directory.rglob("*")]:
        if path.is_dir() and not path.is_symlink():
            path.chmod(0o755)
    return ca


def gateway_certs(certs: Path, domain: str) -> dict[str, Any]:
    """Replaces the gateway's volume subpath mount with the test certificate."""
    return {"volumes": [f"{certs / 'gateways' / domain}:/certs:ro"]}


def _secrets(directory: Path) -> None:
    compose = yaml.safe_load(COMPOSE.read_text())
    for secret in compose["secrets"].values():
        relative = secret["file"].split("}/", 1)[1]
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(uuid.uuid4().hex)


def _fake(script: Path, certs: Path, aliases: list[str], name: str, *ports: str) -> dict[str, Any]:
    return {
        "image": PYTHON,
        "command": ["python", "/fakes.py", "tls", *ports],
        "environment": {"FAKE_NAME": name},
        "networks": {"net-egress": {"aliases": aliases}},
        "volumes": [f"{script}:/fakes.py:ro", f"{certs}:/certs:ro"],
    }


def _probe(
    networks: list[str], extra_hosts: list[str] | None = None, certs: Path | None = None
) -> dict[str, Any]:
    service: dict[str, Any] = {
        "image": PYTHON,
        "command": ["sleep", "infinity"],
        "networks": networks,
        "volumes": [f"{HERE / 'probe.py'}:/probe.py:ro"],
    }
    if certs is not None:
        service["volumes"].append(f"{certs / 'agents'}:/certs:ro")
    if extra_hosts:
        service["extra_hosts"] = extra_hosts
    return service


def _override(tmp: Path) -> tuple[Path, WorkloadCa]:
    certs = tmp / "certs"
    certs.mkdir()
    _certificate(certs)
    workload = tmp / "workload"
    ca = workload_certs(workload, "content")
    tools = tmp / "content-tools.json"
    tools.write_text(
        json.dumps(
            {
                "domain_tools": {
                    "domain": "content",
                    "endpoints": ["test"],
                    "tools": ["allowed_tool"],
                }
            }
        )
    )
    tools.chmod(0o644)
    fakes = HERE / "fakes.py"
    private_hosts = [f"{h}:{PRIVATE_PROXY_IP}" for h in (CALDAV, IMAP, "other.jarvis.test")]
    override = {
        "services": {
            "opa-content": {"volumes": [f"{tools}:/policies/data.json:ro"]},
            "mcp-gateway-content": gateway_certs(workload, "content"),
            "mcp-gateway-trading": gateway_certs(workload, "trading"),
            "fake-telegram": _fake(fakes, certs, ["api.telegram.org"], "telegram", "443"),
            "fake-other": _fake(fakes, certs, ["evil.jarvis.test"], "other", "443"),
            "fake-instagram": _fake(
                fakes,
                certs,
                ["www.instagram.com", "scontent-fra5-1.cdninstagram.com"],
                "instagram",
                "443",
            ),
            "fake-caldav": _fake(fakes, certs, [CALDAV], "caldav", "443"),
            "fake-imap": _fake(fakes, certs, [IMAP], "imap", "993"),
            "fake-unlisted-private": _fake(fakes, certs, ["other.jarvis.test"], "other", "443"),
            "mcp-fake-content": {
                "image": PYTHON,
                "command": ["python", "/fakes.py", "mcp"],
                "networks": {"net-mcp-content": {"aliases": ["mcp-upstream"]}},
                "volumes": [f"{fakes}:/fakes.py:ro"],
            },
            "probe-agent-content": _probe(["net-content"], certs=workload),
            "probe-n8n-content": _probe(["net-mcp-content"]),
            "probe-sandbox": _probe(["net-sandbox-egress"]),
            "probe-n8n-private": _probe(["net-mcp-private"], private_hosts),
            "probe-agent-private": _probe(["net-private"]),
        },
        "networks": {"net-egress": {"ipam": {"config": [{"subnet": "198.18.0.0/24"}]}}},
    }
    path = tmp / "compose.override.yml"
    path.write_text(yaml.safe_dump(override))
    return path, ca


class Stack:
    def __init__(self, project: str, files: list[Path], env: dict[str, str]) -> None:
        self.project = project
        self.files = files
        self.env = env
        self.workload: tuple[Path, WorkloadCa] | None = None  # see workload_certs

    def compose(self, *args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
        cmd = ["docker", "compose", "-p", self.project]
        for file in self.files:
            cmd += ["-f", str(file)]
        return subprocess.run(  # noqa: S603
            [*cmd, *args], env=self.env, capture_output=True, text=True, timeout=timeout
        )

    def probe(self, service: str, *args: str) -> dict[str, Any]:
        proc = subprocess.run(  # noqa: S603
            ["docker", "exec", f"{self.project}-{service}-1", "python", "/probe.py", *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        result: dict[str, Any] = json.loads(proc.stdout)
        return result

    def ip(self, service: str, network: str) -> str:
        proc = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "docker",
                "inspect",
                "-f",
                f'{{{{(index .NetworkSettings.Networks "{self.project}_{network}").IPAddress}}}}',
                f"{self.project}-{service}-1",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip()


def _wait(check: Any, what: str, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(1)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    if shutil.which("docker") is None:
        pytest.skip("docker not installed")
    tmp = tmp_path_factory.mktemp("isolation")
    secrets_dir = tmp / "secrets"
    _secrets(secrets_dir)
    env = {
        **os.environ,
        "JARVIS_SECRETS_DIR": str(secrets_dir),
        "JARVIS_CALDAV_HOST": CALDAV,
        "JARVIS_IMAP_HOST": IMAP,
    }
    project = f"jarvis-iso-{uuid.uuid4().hex[:8]}"
    override, ca = _override(tmp)
    stack = Stack(project, [COMPOSE, override], env)
    stack.workload = (tmp / "workload", ca)
    try:
        up = stack.compose("up", "-d", *SERVICES)
        assert up.returncode == 0, up.stderr

        def gateway_ready() -> bool:
            body = '{"jsonrpc":"2.0","id":1,"method":"ping"}'
            url = "https://mcp-gateway-content:8443/mcp/test"
            result = stack.probe("probe-agent-content", "http", "POST", url, body)
            return bool(result["status"] == 200)

        def proxies_ready() -> bool:
            return bool(
                stack.probe("probe-n8n-content", "tcp", "egress-content", "3128")["ok"]
                and stack.probe("probe-sandbox", "tcp", "egress-reel", "3128")["ok"]
                and stack.probe("probe-n8n-private", "tcp", PRIVATE_PROXY_IP, "443")["ok"]
            )

        _wait(gateway_ready, "policy gateway")
        _wait(proxies_ready, "egress proxies")
        yield stack
    finally:
        stack.compose("down", "-v", "--remove-orphans", timeout=300)


# --- egress: agents have none; n8n and sandboxes only through their allowlist -------------------


def test_agent_network_has_no_egress_and_no_proxy(stack: Stack) -> None:
    fake = stack.ip("fake-telegram", "net-egress")
    assert not stack.probe("probe-agent-content", "tcp", fake, "443")["ok"]
    assert not stack.probe("probe-agent-content", "tcp", "api.telegram.org", "443")["ok"]
    assert not stack.probe("probe-agent-content", "tcp", "egress-content", "3128")["ok"]


def test_n8n_reaches_allowlisted_host_only_through_its_proxy(stack: Stack) -> None:
    probe = "probe-n8n-content"
    via = ("egress-content", "3128")
    assert stack.probe(probe, "connect", *via, "api.telegram.org", "443")["status"] == 200
    assert stack.probe(probe, "connect", *via, "evil.jarvis.test", "443")["status"] == 403
    assert stack.probe(probe, "connect", *via, "api.telegram.org", "80")["status"] == 403
    fake = stack.ip("fake-telegram", "net-egress")
    assert stack.probe(probe, "connect", *via, fake, "443")["status"] == 403
    assert not stack.probe(probe, "tcp", fake, "443")["ok"]


def test_proxy_refuses_plain_http(stack: Stack) -> None:
    result = stack.probe(
        "probe-n8n-content", "http", "GET", "http://egress-content:3128/http://api.telegram.org/"
    )
    assert result["status"] in {400, 403}


def test_reel_sandbox_reaches_instagram_only(stack: Stack) -> None:
    via = ("egress-reel", "3128")
    probe = "probe-sandbox"
    assert stack.probe(probe, "connect", *via, "www.instagram.com", "443")["status"] == 200
    cdn = "scontent-fra5-1.cdninstagram.com"
    assert stack.probe(probe, "connect", *via, cdn, "443")["status"] == 200
    assert stack.probe(probe, "connect", *via, "api.telegram.org", "443")["status"] == 403


def test_reel_sandbox_cannot_use_another_domains_proxy(stack: Stack) -> None:
    assert not stack.probe("probe-sandbox", "tcp", "egress-content", "3128")["ok"]


# --- private: CalDAV and IMAP only, TLS end to end --------------------------------------------


def test_private_reaches_caldav_and_imap(stack: Stack) -> None:
    caldav = stack.probe("probe-n8n-private", "tls", CALDAV, "443", CALDAV)
    assert caldav == {"ok": True, "banner": "ok caldav", "error": ""}
    imap = stack.probe("probe-n8n-private", "tls", IMAP, "993", IMAP)
    assert imap == {"ok": True, "banner": "ok imap", "error": ""}


def test_private_refuses_other_hosts_and_ports(stack: Stack) -> None:
    probe = "probe-n8n-private"
    assert not stack.probe(probe, "tls", "other.jarvis.test", "443", "other.jarvis.test")["ok"]
    assert not stack.probe(probe, "tls", CALDAV, "993", CALDAV)["ok"]
    assert not stack.probe(probe, "tls", PRIVATE_PROXY_IP, "443", "api.telegram.org")["ok"]
    assert not stack.probe(probe, "tcp", PRIVATE_PROXY_IP, "25")["ok"]
    fake = stack.ip("fake-caldav", "net-egress")
    assert not stack.probe(probe, "tcp", fake, "443")["ok"]


def test_private_agents_have_no_route_at_all(stack: Stack) -> None:
    assert not stack.probe("probe-agent-private", "tcp", CALDAV, "443")["ok"]
    assert not stack.probe("probe-agent-private", "tcp", PRIVATE_PROXY_IP, "443")["ok"]


# --- policy gateway: agents reach MCP tools only through Envoy + OPA ---------------------------


def _rpc(method: str, **params: Any) -> str:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params:
        body["params"] = params
    return json.dumps(body)


GATEWAY = "https://mcp-gateway-content:8443"


def test_gateway_forwards_listed_tool_with_domain(stack: Stack) -> None:
    url = f"{GATEWAY}/mcp/test"
    call = _rpc("tools/call", name="allowed_tool", arguments={"x": 1})
    result = stack.probe("probe-agent-content", "http", "POST", url, call)
    assert result["status"] == 200
    assert json.loads(result["body"])["result"] == {"echo": "tools/call", "domain": "content"}


def test_gateway_refuses_everything_else(stack: Stack) -> None:
    probe = "probe-agent-content"
    url = f"{GATEWAY}/mcp/test"
    unlisted = _rpc("tools/call", name="publish_post", arguments={})
    assert stack.probe(probe, "http", "POST", url, unlisted)["status"] == 403
    resources = _rpc("resources/read", uri="file:///etc/passwd")
    assert stack.probe(probe, "http", "POST", url, resources)["status"] == 403
    assert stack.probe(probe, "http", "GET", url)["status"] == 405
    other = f"{GATEWAY}/mcp/other"
    assert stack.probe(probe, "http", "POST", other, _rpc("ping"))["status"] == 403
    escaped = f"{GATEWAY}/mcp/test%2F..%2F..%2Frest"
    assert stack.probe(probe, "http", "POST", escaped, _rpc("ping"))["status"] in {400, 403}
    assert stack.probe(probe, "http", "POST", f"{GATEWAY}/rest/login", "{}")["status"] in {
        403,
        404,
    }
    oversized = _rpc("tools/call", name="allowed_tool", arguments={"x": "a" * 70_000})
    assert stack.probe(probe, "http", "POST", url, oversized)["status"] in {403, 413}


def test_agents_cannot_bypass_the_gateway(stack: Stack) -> None:
    probe = "probe-agent-content"
    assert not stack.probe(probe, "tcp", "mcp-upstream", "5678")["ok"]
    assert not stack.probe(probe, "tcp", "mcp-fake-content", "5678")["ok"]
    assert not stack.probe(probe, "tcp", "opa-content", "9191")["ok"]
    assert not stack.probe(probe, "tcp", "mcp-gateway-trading", "8443")["ok"]


def _as(stack: Stack, certs: str, *args: str) -> dict[str, Any]:
    """The content agent probe with another client certificate (see workload_certs)."""
    proc = subprocess.run(  # noqa: S603
        ["docker", "exec", "-e", f"PROBE_CERTS={certs}",  # noqa: S607
         f"{stack.project}-probe-agent-content-1", "python", "/probe.py", *args],
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    result: dict[str, Any] = json.loads(proc.stdout)
    return result


def test_gateway_requires_a_workload_certificate_of_its_domain(stack: Stack) -> None:
    url = f"{GATEWAY}/mcp/test"
    call = _rpc("tools/call", name="allowed_tool", arguments={})
    assert _as(stack, "/certs/ok", "http", "POST", url, call)["status"] == 200
    # Envoy refuses the handshake: no certificate, an expired one, or one from another CA.
    for certs in ("none", "/certs/expired", "/certs/rogue"):
        assert _as(stack, certs, "http", "POST", url, call)["status"] == 0, certs
    # A valid certificate of another domain gets through TLS, and OPA refuses it.
    assert _as(stack, "/certs/other-domain", "http", "POST", url, call)["status"] == 403
    # Plain HTTP is not spoken at all.
    assert (
        stack.probe(
            "probe-agent-content", "http", "POST", "http://mcp-gateway-content:8443/mcp/test", call
        )["status"]
        == 0
    )


def test_gateway_reloads_a_renewed_certificate(stack: Stack) -> None:
    """pki-issue renews every 8 hours; the gateway must serve the new certificate untouched."""
    assert stack.workload is not None
    directory, ca = stack.workload
    before = _served_serial(stack)
    assert before
    gateway = directory / "gateways" / "content"
    ca.issue(gateway, "content", "mcp-gateway", dns="mcp-gateway-content", rotate=True)
    _wait(lambda: _served_serial(stack) not in {before, ""}, "the renewed gateway certificate", 60)
    url = f"{GATEWAY}/mcp/test"
    assert _as(stack, "/certs/ok", "http", "POST", url, _rpc("ping"))["status"] == 200


def _served_serial(stack: Stack) -> str:
    result = stack.probe("probe-agent-content", "serial", "mcp-gateway-content", "8443")
    return str(result["serial"])


def test_decision_logs_hold_no_tool_arguments(stack: Stack) -> None:
    marker = f"secret-argument-{uuid.uuid4().hex}"
    url = f"{GATEWAY}/mcp/test"
    call = _rpc("tools/call", name="allowed_tool", arguments={"text": marker})
    assert stack.probe("probe-agent-content", "http", "POST", url, call)["status"] == 200
    logs = subprocess.run(  # noqa: S603
        ["docker", "logs", f"{stack.project}-opa-content-1"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    output = logs.stdout + logs.stderr
    assert "allowed_tool" in output
    assert marker not in output


def test_opa_rest_api_is_not_exposed(stack: Stack) -> None:
    assert not stack.probe("probe-n8n-content", "tcp", "opa-content", "8181")["ok"]


def test_gateway_fails_closed_without_opa(stack: Stack) -> None:
    """Runs last in this module: it stops the content domain's OPA."""
    assert stack.compose("stop", "opa-content").returncode == 0
    url = f"{GATEWAY}/mcp/test"
    call = _rpc("tools/call", name="allowed_tool", arguments={})
    status = stack.probe("probe-agent-content", "http", "POST", url, call)["status"]
    assert status in {403, 503}
