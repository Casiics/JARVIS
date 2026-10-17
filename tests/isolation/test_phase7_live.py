"""Phase 7 services in Docker: the audit keeper against a real pg-audit, and the workload CA.

The keeper signs the chain's head, exports a sealed bundle and verifies both with only the
public key. The CA is initialised without network, issues 24-hour certificates with SPIFFE IDs
into each domain's own volume, and is reachable from no domain network.

Opt in with JARVIS_TEST_DOCKER=1. Builds the keeper image if it is not present.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import yaml

from tests.isolation.test_isolation_live import COMPOSE, HERE, PYTHON, Stack, _secrets, _wait

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("JARVIS_TEST_DOCKER") != "1", reason="JARVIS_TEST_DOCKER=1 not set"
    ),
]

KEEPER = "localhost/jarvis-audit-keeper:0.1.0"
WRITE_ENTRIES = """
import psycopg
from jarvis_core.audit.chain import AuditWriter
from jarvis_core.audit.postgres import PostgresAuditStore
from jarvis_core.contracts.audit import AuditAction, AuditDecision, AuditLogEntry
from jarvis_core.contracts.classification import Domain
password = open('/run/secrets/pg_audit_writer_password').read().strip()
conn = psycopg.connect('host=pg-audit dbname=jarvis user=audit_writer', password=password)
writer = AuditWriter(PostgresAuditStore(conn))
for n in range(3):
    writer.write(AuditLogEntry(trace_id=f't{n}', domain=Domain.TRADING, actor='live-test',
                               action=AuditAction.GATE_DECISION, target='risk_gate',
                               decision=AuditDecision.DENY))
print('wrote 3')
"""


def _subpath(volume: str, subpath: str, target: str) -> dict[str, Any]:
    return {
        "type": "volume",
        "source": volume,
        "target": target,
        "read_only": True,
        "volume": {"subpath": subpath},
    }


def run(stack: Stack, *args: str) -> subprocess.CompletedProcess[str]:
    return stack.compose("run", "--rm", "-T", *args, timeout=300)


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    tmp = tmp_path_factory.mktemp("phase7")
    secrets_dir = tmp / "secrets"
    _secrets(secrets_dir)
    (secrets_dir / "core/audit_signing_key").write_text(secrets.token_hex(32))
    override = tmp / "override.yml"
    override.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "probe-core": {
                        "image": PYTHON,
                        "command": ["sleep", "infinity"],
                        "networks": ["net-core"],
                        "volumes": [f"{HERE / 'probe.py'}:/probe.py:ro"],
                    },
                    "probe-agent-trading": {
                        "image": PYTHON,
                        "command": ["sleep", "infinity"],
                        "networks": ["net-trading"],
                        "volumes": [f"{HERE / 'probe.py'}:/probe.py:ro"],
                    },
                    # Started after pki-issue: the subpaths exist only then. Real certificates:
                    # the trading client's, and the content gateway's as a foreign caller.
                    "probe-mtls": {
                        "image": PYTHON,
                        "command": ["sleep", "infinity"],
                        "networks": ["net-trading"],
                        "profiles": ["mtls"],
                        "volumes": [
                            f"{HERE / 'probe.py'}:/probe.py:ro",
                            _subpath("certs_trading", "paper", "/certs/ok"),
                            _subpath("certs_content", "mcp-gateway", "/certs/other-domain"),
                        ],
                    },
                }
            }
        )
    )
    env = {**os.environ, "JARVIS_SECRETS_DIR": str(secrets_dir)}
    stack = Stack(f"jarvis-p7-{uuid.uuid4().hex[:8]}", [COMPOSE, override], env)
    try:
        has_image = subprocess.run(  # noqa: S603
            ["docker", "image", "inspect", KEEPER],  # noqa: S607
            capture_output=True,
            check=False,
        )
        if has_image.returncode != 0:
            build = stack.compose("build", "audit-keeper", timeout=900)
            assert build.returncode == 0, build.stderr
        up = stack.compose("up", "-d", "pg-audit", "probe-core", "probe-agent-trading")
        assert up.returncode == 0, up.stderr
        _wait(
            lambda: stack.probe("probe-core", "tcp", "pg-audit", "5432")["ok"],
            "pg-audit",
        )
        _wait(
            lambda: (
                run(stack, "--entrypoint", "python", "audit-keeper", "-c", WRITE_ENTRIES)
                .stdout.strip()
                .endswith("wrote 3")
            ),
            "pg-audit accepting the writer",
            timeout=180,
        )
        yield stack
    finally:
        stack.compose("down", "-v", "--remove-orphans", timeout=300)


@pytest.fixture(scope="module")
def public_key(stack: Stack) -> str:
    result = run(stack, "audit-keeper", "public-key")
    assert result.returncode == 0, result.stderr
    key = result.stdout.strip().splitlines()[-1]
    assert len(key) == 64
    return key


def test_keeper_signs_exports_and_verifies(stack: Stack, public_key: str) -> None:
    signed = run(stack, "audit-keeper", "checkpoint")
    assert signed.returncode == 0, signed.stderr
    assert "signed 3 entries" in signed.stdout
    exported = run(stack, "audit-keeper", "export")
    assert "exported entries 1-3" in exported.stdout, exported.stderr
    again = run(stack, "audit-keeper", "export")
    assert "nothing new to export" in again.stdout
    verified = run(stack, "audit-keeper", "verify", "--public-key", public_key)
    assert verified.returncode == 0 and "log intact" in verified.stdout, verified.stderr
    bundles = run(stack, "audit-keeper", "verify-bundles", "--public-key", public_key)
    assert bundles.returncode == 0 and "bundles intact" in bundles.stdout, bundles.stderr


def test_wrong_public_key_fails_verification(stack: Stack, public_key: str) -> None:
    run(stack, "audit-keeper", "checkpoint")
    other = secrets.token_hex(32)  # not a real key of ours; may not even be on the curve
    result = run(stack, "audit-keeper", "verify", "--public-key", other)
    assert result.returncode != 0


def test_bundles_are_read_only_in_the_volume(stack: Stack, public_key: str) -> None:
    run(stack, "audit-keeper", "checkpoint")
    run(stack, "audit-keeper", "export")
    listing = run(stack, "--entrypoint", "sh", "audit-keeper", "-c", "ls -l /worm")
    lines = [line for line in listing.stdout.splitlines() if "audit-" in line]
    assert lines and all(line.startswith("-r--r--r--") for line in lines), listing.stdout
    overwrite = run(
        stack, "--entrypoint", "sh", "audit-keeper", "-c", "echo x > /worm/$(ls /worm | head -1)"
    )
    assert overwrite.returncode != 0


# --- workload CA ----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pki(stack: Stack) -> Stack:
    init = run(stack, "pki-init")
    assert init.returncode == 0 and "initialised" in init.stdout, init.stderr
    assert "password" not in (init.stdout + init.stderr).lower()
    up = stack.compose("up", "-d", "pki-ca")
    assert up.returncode == 0, up.stderr

    def issued() -> bool:
        result = run(stack, "pki-issue")
        return result.returncode == 0 and "issued spiffe://jarvis.home/trading/executor" in (
            result.stdout
        )

    _wait(issued, "the workload CA issuing certificates", timeout=180)
    return stack


def inspect(stack: Stack, domain: str, command: str) -> str:
    result = run(
        stack,
        "--entrypoint",
        "sh",
        # Root owns the issued files (0600); root without capabilities can still read them.
        "--user",
        "0:0",
        "-e",
        "STEPPATH=/tmp/step",
        "-v",
        f"{stack.project}_certs_{domain}:/c:ro",
        "pki-init",
        "-c",
        command,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_certificates_carry_spiffe_ids_and_live_a_day(pki: Stack) -> None:
    text = inspect(
        pki, "trading", "step certificate inspect /c/executor/tls.crt --format json --bundle"
    )
    certificate = json.loads(text)
    assert "spiffe://jarvis.home/trading/executor" in text
    leaf = certificate[0] if isinstance(certificate, list) else certificate
    assert leaf["validity"]["length"] <= 24 * 3600 + 60  # step-ca backdates 1 min
    verify = inspect(
        pki,
        "trading",
        "step certificate verify /c/executor/tls.crt --roots /c/executor/root_ca.crt && echo ok",
    )
    assert verify.strip().endswith("ok")


def test_each_domain_volume_holds_only_its_own_identities(pki: Stack) -> None:
    trading = inspect(pki, "trading", "ls /c").split()
    assert sorted(trading) == ["executor", "mcp-gateway", "paper"]
    files = inspect(pki, "trading", "ls /c/mcp-gateway").split()
    assert sorted(files) == ["current", "root_ca.crt", "tls.crt", "tls.key"]
    private = inspect(pki, "private", "ls /c").split()
    assert not any(name.startswith("executor") for name in private)


def test_no_domain_network_reaches_the_ca(pki: Stack) -> None:
    assert not pki.probe("probe-core", "tcp", "pki-ca", "9000")["ok"]
    assert not pki.probe("probe-agent-trading", "tcp", "pki-ca", "9000")["ok"]


def test_each_certificate_belongs_to_its_consumer(pki: Stack) -> None:
    # Envoy runs as 101, the trading client as 10001; nobody else may read the keys.
    listing = inspect(
        pki, "trading", "stat -L -c '%n %u %a' /c/mcp-gateway/tls.key /c/paper/tls.key"
    )
    assert listing.split("\n")[:2] == [
        "/c/mcp-gateway/tls.key 101 400",
        "/c/paper/tls.key 10001 400",
    ]


def _served_serial(stack: Stack) -> str:
    result = stack.probe("probe-mtls", "serial", "mcp-gateway-trading", "8443")
    return str(result["serial"])


def _as(stack: Stack, certs: str, *args: str) -> dict[str, Any]:
    proc = subprocess.run(  # noqa: S603
        ["docker", "exec", "-e", f"PROBE_CERTS={certs}",  # noqa: S607
         f"{stack.project}-probe-mtls-1", "python", "/probe.py", *args],
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    result: dict[str, Any] = json.loads(proc.stdout)
    return result


def test_gateway_uses_issued_certificates_and_reloads_renewals(pki: Stack) -> None:
    """The real chain: step-ca certificates, subpath mounts, Envoy as uid 101, and OPA."""
    up = pki.compose(
        "--profile", "mtls", "up", "-d", "opa-trading", "mcp-gateway-trading", "probe-mtls"
    )
    assert up.returncode == 0, up.stderr
    _wait(lambda: _served_serial(pki) != "", "the trading gateway on mTLS", timeout=120)
    url = "https://mcp-gateway-trading:8443/mcp/marketdata"
    ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    # Allowed by OPA; n8n-trading is not running, so Envoy answers for the missing upstream.
    assert _as(pki, "/certs/ok", "http", "POST", url, ping)["status"] == 503
    assert _as(pki, "/certs/other-domain", "http", "POST", url, ping)["status"] == 403
    assert _as(pki, "none", "http", "POST", url, ping)["status"] == 0
    before = _served_serial(pki)
    renewed = run(pki, "pki-issue")
    assert renewed.returncode == 0, renewed.stderr
    _wait(lambda: _served_serial(pki) not in {before, ""}, "the renewed certificate", timeout=60)
    assert _as(pki, "/certs/ok", "http", "POST", url, ping)["status"] == 503
