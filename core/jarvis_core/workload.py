"""Mutual TLS with workload certificates from the workload CA (docker/pki/).

pki-issue writes each identity's files into one directory that only its consumer mounts:
`tls.crt` (leaf and intermediate), `tls.key` and `root_ca.crt`. They are replaced every
8 hours, so a client builds its TLS context per connection and never caches the key.

The server is checked twice: its certificate must chain to the workload root (no public CA is
trusted), match the host name, and carry the expected SPIFFE ID, so a certificate issued to
another service with a similar name is refused.
"""

from __future__ import annotations

import http.client
import re
import ssl
from pathlib import Path

TRUST_DOMAIN = "jarvis.home"
DEFAULT_CERT_DIR = Path("/certs")
SPIFFE_ID = re.compile(
    r"^spiffe://jarvis\.home/(core|trading|content|private)/[a-z][a-z0-9-]{0,39}$"
)


class WorkloadTlsError(Exception):
    """No usable workload certificate, or the peer is not who it should be."""


def spiffe_id(domain: str, service: str) -> str:
    sid = f"spiffe://{TRUST_DOMAIN}/{domain}/{service}"
    if not SPIFFE_ID.match(sid):
        raise ValueError(f"not a workload identity: {sid}")
    return sid


def client_context(cert_dir: Path) -> ssl.SSLContext:
    """TLS 1.3 client context that trusts only the workload root and presents our certificate."""
    try:
        context = ssl.create_default_context(cafile=str(cert_dir / "root_ca.crt"))
        context.load_cert_chain(cert_dir / "tls.crt", cert_dir / "tls.key")
    except (OSError, ssl.SSLError) as exc:
        raise WorkloadTlsError(f"no usable workload certificate in {cert_dir}") from exc
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    return context


def peer_ids(sock: ssl.SSLSocket) -> list[str]:
    cert = sock.getpeercert() or {}
    sans: tuple[tuple[str, str], ...] = cert.get("subjectAltName", ())  # type: ignore[assignment]
    return [value for kind, value in sans if kind == "URI"]


class WorkloadConnection(http.client.HTTPSConnection):
    """HTTPS connection that refuses a server without the expected SPIFFE ID."""

    def __init__(
        self, host: str, port: int, *, cert_dir: Path, peer: str, timeout: float = 30
    ) -> None:
        if not SPIFFE_ID.match(peer):
            raise ValueError(f"not a workload identity: {peer}")
        super().__init__(host, port, timeout=timeout, context=client_context(cert_dir))
        self._peer = peer

    def connect(self) -> None:
        try:
            super().connect()
        except (ssl.SSLError, ssl.CertificateError) as exc:
            raise WorkloadTlsError(f"TLS handshake with {self.host} failed") from exc
        if not isinstance(self.sock, ssl.SSLSocket) or peer_ids(self.sock) != [self._peer]:
            self.sock.close()
            raise WorkloadTlsError(f"{self.host} is not {self._peer}")
