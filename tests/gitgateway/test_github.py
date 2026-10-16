from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from jarvis_core.gitgateway.github import (
    GitHubApp,
    GitHubError,
    GitHubPullRequests,
    Repository,
    app_jwt,
    urllib_transport,
)

NOW = datetime(2026, 10, 16, 8, 0, 0, tzinfo=UTC)
REPO = Repository("Casiics", "JARVIS")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


class Recorder:
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body
        self.calls: list[tuple[str, str, dict[str, str], dict[str, Any] | None]] = []

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: dict[str, Any] | None
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append((method, url, headers, body))
        return self.status, self.body


def test_jwt_is_rs256_short_lived_and_verifiable(key: rsa.RSAPrivateKey) -> None:
    token = app_jwt("12345", pem(key), NOW)
    header, claims, signature = token.split(".")
    assert json.loads(unb64(header)) == {"alg": "RS256", "typ": "JWT"}
    body = json.loads(unb64(claims))
    assert body["iss"] == "12345"
    assert body["exp"] - body["iat"] == 600
    assert body["iat"] == int(NOW.timestamp()) - 60
    key.public_key().verify(
        unb64(signature), f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )


def test_non_rsa_key_is_refused() -> None:
    other = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    with pytest.raises(GitHubError):
        app_jwt("1", other, NOW)


def test_installation_token_is_scoped_to_one_repo(key: rsa.RSAPrivateKey) -> None:
    transport = Recorder(201, {"token": "ghs_x"})
    app = GitHubApp("1", "99", pem(key), REPO, transport=transport, clock=lambda: NOW)
    assert app.installation_token() == "ghs_x"
    [(method, url, headers, body)] = transport.calls
    assert method == "POST"
    assert url == "https://api.github.com/app/installations/99/access_tokens"
    assert headers["Authorization"].startswith("Bearer ey")
    assert body == {
        "repositories": ["JARVIS"],
        "permissions": {"contents": "write", "pull_requests": "write"},
    }


def test_installation_token_failure_raises(key: rsa.RSAPrivateKey) -> None:
    app = GitHubApp("1", "99", pem(key), REPO, transport=Recorder(404, {}), clock=lambda: NOW)
    with pytest.raises(GitHubError):
        app.installation_token()


def test_pull_requests_are_drafts() -> None:
    transport = Recorder(201, {"html_url": "https://github.com/Casiics/JARVIS/pull/9"})
    pulls = GitHubPullRequests(REPO, transport=transport)
    url = pulls.open(token="ghs_x", head="auto-improve/x", base="main", title="t", body="b")  # noqa: S106
    assert url.endswith("/pull/9")
    [(_, call_url, headers, body)] = transport.calls
    assert call_url == "https://api.github.com/repos/Casiics/JARVIS/pulls"
    assert headers["Authorization"] == "Bearer ghs_x"
    assert body is not None and body["draft"] is True


def test_transport_refuses_other_hosts() -> None:
    with pytest.raises(GitHubError):
        urllib_transport("GET", "https://evil.example/app", {}, None)


def test_private_key_is_not_in_repr(key: rsa.RSAPrivateKey) -> None:
    app = GitHubApp("1", "99", pem(key), REPO)
    assert "PRIVATE KEY" not in repr(app)
