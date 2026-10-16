"""GitHub App authentication and pull requests for the git gateway.

No personal access token exists (docs/architecture.md, section 7.2). The gateway signs a
short JWT with the App's private key, exchanges it for an installation token limited to this
one repository with `contents` and `pull_requests` write, and uses that token for one push and
one PR. Tokens never leave this module except as a git http header for the push.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

API = "https://api.github.com"


class GitHubError(Exception):
    pass


class Transport(Protocol):
    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: dict[str, Any] | None
    ) -> tuple[int, dict[str, Any]]: ...


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: dict[str, Any] | None
) -> tuple[int, dict[str, Any]]:
    if not url.startswith(API + "/"):
        raise GitHubError("refusing a non-GitHub URL")
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def app_jwt(app_id: str, private_key_pem: bytes, now: datetime) -> str:
    """RS256 JWT for the GitHub App: issued a minute in the past, valid for nine minutes."""
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise GitHubError("GitHub App keys are RSA")
    header = {"alg": "RS256", "typ": "JWT"}
    issued = int((now - timedelta(seconds=60)).timestamp())
    claims = {"iat": issued, "exp": issued + 600, "iss": app_id}
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode()) for part in (header, claims)
    )
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(signature)}"


def _headers(authorization: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": authorization,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "jarvis-git-gateway",
    }


@dataclass(frozen=True)
class Repository:
    owner: str
    name: str

    @property
    def https_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}.git"


@dataclass
class GitHubApp:
    app_id: str
    installation_id: str
    private_key_pem: bytes = field(repr=False)
    repository: Repository
    transport: Transport = urllib_transport
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def installation_token(self) -> str:
        jwt = app_jwt(self.app_id, self.private_key_pem, self.clock())
        status, body = self.transport(
            "POST",
            f"{API}/app/installations/{self.installation_id}/access_tokens",
            _headers(f"Bearer {jwt}"),
            {
                "repositories": [self.repository.name],
                "permissions": {"contents": "write", "pull_requests": "write"},
            },
        )
        token = body.get("token")
        if status != 201 or not isinstance(token, str):
            raise GitHubError(f"installation token request failed with {status}")
        return token


@dataclass
class GitHubPullRequests:
    """Opens draft PRs with an installation token."""

    repository: Repository
    transport: Transport = urllib_transport

    def open(self, *, token: str, head: str, base: str, title: str, body: str) -> str:
        status, response = self.transport(
            "POST",
            f"{API}/repos/{self.repository.owner}/{self.repository.name}/pulls",
            _headers(f"Bearer {token}"),
            {"head": head, "base": base, "title": title, "body": body, "draft": True},
        )
        url = response.get("html_url")
        if status != 201 or not isinstance(url, str):
            raise GitHubError(f"opening the pull request failed with {status}")
        return url
