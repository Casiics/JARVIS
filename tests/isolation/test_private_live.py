"""The private domain end to end in Docker (Phase 5 exit criterion).

Runs the real private-assistant image with `daily` against stand-ins for a CalDAV server, an
IMAP server and Ollama. The calendar and mailbox sit on net-egress under their real host names
behind egress-private; TLS is verified against a throwaway CA. Then probes from the assistant's
network position show that it reaches only those two hosts and Ollama.

Opt in with JARVIS_TEST_DOCKER=1. Builds the image from modules/mod-private/Dockerfile if it is
not present.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
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

from tests.isolation.test_isolation_live import (
    CALDAV,
    COMPOSE,
    HERE,
    IMAP,
    PYTHON,
    Stack,
    _secrets,
    _wait,
)

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("JARVIS_TEST_DOCKER") != "1", reason="JARVIS_TEST_DOCKER=1 not set"
    ),
]

IMAGE = "localhost/jarvis-private:0.1.0"
ASSISTANT_PROXY_IP = "10.213.41.2"
N8N_PROXY_IP = "10.213.40.2"
SERVICES = [
    "egress-private",
    "fake-caldav",
    "fake-imap",
    "fake-unlisted-private",
    "fake-ollama",
    "probe-assistant",
]


def _pem(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o644)


def _ca_and_leaf(directory: Path) -> None:
    """A throwaway CA (trusted by the assistant) and a leaf for the CalDAV and IMAP hosts."""
    now = dt.datetime.now(dt.UTC)
    validity = (now - dt.timedelta(minutes=5), now + dt.timedelta(hours=2))
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "jarvis-test-ca")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(validity[0])
        .not_valid_after(validity[1])
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CALDAV)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(validity[0])
        .not_valid_after(validity[1])
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(CALDAV), x509.DNSName(IMAP)]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    _pem(directory / "ca.pem", ca.public_bytes(serialization.Encoding.PEM))
    _pem(directory / "cert.pem", leaf.public_bytes(serialization.Encoding.PEM))
    _pem(
        directory / "key.pem",
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def _fake(certs: Path, mode: str, networks: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    return {
        "image": PYTHON,
        "command": ["python", "/fakes.py", mode],
        "environment": env,
        "networks": networks,
        "volumes": [f"{HERE / 'fakes.py'}:/fakes.py:ro", f"{certs}:/certs:ro"],
    }


class PrivateStack(Stack):
    def __init__(self, project: str, files: list[Path], env: dict[str, str]) -> None:
        super().__init__(project, files, env)
        self.credentials: dict[str, str] = {}

    def assistant(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.compose("run", "--rm", "-T", "private-assistant", *args, timeout=300)

    def log_lines(self, service: str) -> list[dict[str, Any]]:
        out = self.compose("logs", "--no-log-prefix", service).stdout
        return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PrivateStack]:
    tmp = tmp_path_factory.mktemp("private")
    secrets_dir = tmp / "secrets"
    _secrets(secrets_dir)
    private = secrets_dir / "private"
    credentials = {
        key: (private / key).read_text()
        for key in ("caldav_username", "caldav_password", "imap_username", "imap_password")
    }
    certs = tmp / "certs"
    certs.mkdir()
    _ca_and_leaf(certs)

    egress = {"net-egress": {"aliases": [CALDAV]}}
    override = {
        "services": {
            "private-assistant": {
                # Trust the throwaway CA; everything else is the production service definition.
                "environment": {"SSL_CERT_FILE": "/certs/ca.pem"},
                "volumes": [f"{certs / 'ca.pem'}:/certs/ca.pem:ro"],
            },
            "fake-caldav": _fake(
                certs,
                "caldav",
                egress,
                {
                    "FAKE_USER": credentials["caldav_username"],
                    "FAKE_PASSWORD": credentials["caldav_password"],
                },
            ),
            "fake-imap": _fake(
                certs,
                "imap",
                {"net-egress": {"aliases": [IMAP]}},
                {
                    "FAKE_USER": credentials["imap_username"],
                    "FAKE_PASSWORD": credentials["imap_password"],
                },
            ),
            "fake-unlisted-private": _fake(
                certs,
                "caldav",
                {"net-egress": {"aliases": ["other.jarvis.test"]}},
                {"FAKE_USER": "x", "FAKE_PASSWORD": "x"},
            ),
            "fake-ollama": _fake(
                certs, "ollama", {"net-inference-private": {"aliases": ["ollama"]}}, {}
            ),
            "probe-assistant": {
                "image": PYTHON,
                "command": ["sleep", "infinity"],
                "networks": ["net-private-assistant", "net-inference-private"],
                "extra_hosts": [
                    f"{h}:{ASSISTANT_PROXY_IP}" for h in (CALDAV, IMAP, "other.jarvis.test")
                ],
                "volumes": [f"{HERE / 'probe.py'}:/probe.py:ro"],
            },
        },
        "networks": {"net-egress": {"ipam": {"config": [{"subnet": "198.18.0.0/24"}]}}},
    }
    override_path = tmp / "override.yml"
    override_path.write_text(yaml.safe_dump(override))
    env = {
        **os.environ,
        "JARVIS_SECRETS_DIR": str(secrets_dir),
        "JARVIS_CALDAV_HOST": CALDAV,
        "JARVIS_IMAP_HOST": IMAP,
        "JARVIS_CALDAV_URL": f"https://{CALDAV}/cal/",
        "JARVIS_OWNER_ADDRESS": "owner@example.org",
    }
    stack = PrivateStack(f"jarvis-priv-{uuid.uuid4().hex[:8]}", [COMPOSE, override_path], env)
    stack.credentials = credentials
    try:
        has_image = subprocess.run(  # noqa: S603
            ["docker", "image", "inspect", IMAGE],  # noqa: S607
            capture_output=True,
            check=False,
        )
        if has_image.returncode != 0:
            build = stack.compose("build", "private-assistant", timeout=900)
            assert build.returncode == 0, build.stderr
        up = stack.compose("up", "-d", *SERVICES)
        assert up.returncode == 0, up.stderr
        _wait(
            lambda: (
                stack.probe("probe-assistant", "tcp", ASSISTANT_PROXY_IP, "443")["ok"]
                and stack.probe("probe-assistant", "tcp", "ollama", "11434")["ok"]
            ),
            "egress-private and the Ollama stand-in",
        )
        yield stack
    finally:
        stack.compose("down", "-v", "--remove-orphans", timeout=300)


# --- the assistant itself -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def daily(stack: PrivateStack) -> subprocess.CompletedProcess[str]:
    return stack.assistant("daily")


def test_daily_syncs_sorts_and_briefs_inside_the_container(
    daily: subprocess.CompletedProcess[str],
) -> None:
    assert daily.returncode == 0, daily.stderr
    assert "synced 1 events, 2 new mails" in daily.stdout
    assert "sorted 2 mails" in daily.stdout
    assert "Ein ruhiger Tag mit einem Termin." in daily.stdout


def test_state_survives_in_the_data_volume(
    stack: PrivateStack, daily: subprocess.CompletedProcess[str]
) -> None:
    listing = stack.assistant("mail")
    assert listing.returncode == 0, listing.stderr
    lines = listing.stdout.splitlines()
    assert len(lines) == 2
    assert all("[fyi]" in line for line in lines)
    again = stack.assistant("sync")
    assert "synced 1 events, 0 new mails" in again.stdout, "the UID cursor was not kept"


def test_mailbox_is_only_read(stack: PrivateStack, daily: subprocess.CompletedProcess[str]) -> None:
    commands = {line["command"] for line in stack.log_lines("fake-imap")}
    assert "EXAMINE" in commands
    assert commands <= {"CAPABILITY", "LOGIN", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT"}


def test_calendar_is_queried_with_credentials(
    stack: PrivateStack, daily: subprocess.CompletedProcess[str]
) -> None:
    reports = stack.log_lines("fake-caldav")
    assert reports
    assert all(r["method"] == "REPORT" and r["auth"] and r["depth"] == "1" for r in reports)


def test_models_never_see_credentials(
    stack: PrivateStack, daily: subprocess.CompletedProcess[str]
) -> None:
    calls = stack.log_lines("fake-ollama")
    assert calls, "the workers did not call the local model"
    for call in calls:
        assert call["path"] == "/api/chat"
        for secret in stack.credentials.values():
            assert secret not in call["request"]


def test_untrusted_certificate_is_refused(stack: PrivateStack) -> None:
    # Same stack without the throwaway CA: TLS verification must fail closed.
    result = stack.compose(
        "run", "--rm", "-T", "-e", "SSL_CERT_FILE=/nonexistent", "private-assistant", "sync"
    )
    assert result.returncode != 0
    assert "CERTIFICATE_VERIFY_FAILED" in result.stderr


# --- network position ---------------------------------------------------------------------------


def test_assistant_reaches_caldav_and_imap_through_its_proxy(stack: PrivateStack) -> None:
    assert stack.probe("probe-assistant", "tls", CALDAV, "443", CALDAV)["ok"]
    assert stack.probe("probe-assistant", "tls", IMAP, "993", IMAP)["ok"]


def test_assistant_reaches_nothing_else(stack: PrivateStack) -> None:
    probe = "probe-assistant"
    assert not stack.probe(probe, "tls", "other.jarvis.test", "443", "other.jarvis.test")["ok"]
    assert not stack.probe(probe, "tls", CALDAV, "993", CALDAV)["ok"]
    fake = stack.ip("fake-caldav", "net-egress")
    assert not stack.probe(probe, "tcp", fake, "443")["ok"]
    # n8n-private's side of the proxy and the n8n network are out of reach.
    assert not stack.probe(probe, "tcp", N8N_PROXY_IP, "443")["ok"]
    assert not stack.probe(probe, "tcp", "n8n-private", "5678")["ok"]
    assert not stack.probe(probe, "tcp", "mcp-gateway-private", "8443")["ok"]
