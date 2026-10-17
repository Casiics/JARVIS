"""Mutual TLS with workload certificates (jarvis_core.workload, mod_trading.marketdata)."""

from __future__ import annotations

import json
import ssl
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from jarvis_core.workload import WorkloadConnection, WorkloadTlsError, client_context, spiffe_id
from mod_trading.marketdata import GATEWAY_ID, MarketDataError, workload_post
from tests.workload_ca import WorkloadCa


class _Server:
    def __init__(self, port: int, seen: list[str]) -> None:
        self.port = port
        self.seen = seen


def _serve(ca: WorkloadCa, tmp: Path, service: str = "mcp-gateway") -> Iterator[_Server]:
    certs = ca.issue(tmp / "server", "trading", service, dns="localhost")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(certs / "tls.crt", certs / "tls.key")
    context.load_verify_locations(certs / "root_ca.crt")
    context.verify_mode = ssl.CERT_REQUIRED
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            assert isinstance(self.connection, ssl.SSLSocket)
            cert = self.connection.getpeercert() or {}
            sans: tuple[tuple[str, str], ...] = cert.get("subjectAltName", ())  # type: ignore[assignment]
            seen.extend(v for k, v in sans if k == "URI")
            self.rfile.read(int(self.headers["content-length"]))
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Server(server.server_address[1], seen)
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def ca() -> WorkloadCa:
    return WorkloadCa()


@pytest.fixture
def gateway(ca: WorkloadCa, tmp_path: Path) -> Iterator[_Server]:
    yield from _serve(ca, tmp_path)


def _post(port: int, certs: Path, peer: str = GATEWAY_ID) -> int:
    conn = WorkloadConnection("localhost", port, cert_dir=certs, peer=peer, timeout=5)
    try:
        conn.request("POST", "/mcp/marketdata", body=b"{}", headers={"content-type": "x"})
        return conn.getresponse().status
    finally:
        conn.close()


def test_client_and_gateway_authenticate_each_other(
    ca: WorkloadCa, gateway: _Server, tmp_path: Path
) -> None:
    certs = ca.issue(tmp_path / "client", "trading", "paper")
    assert _post(gateway.port, certs) == 200
    assert gateway.seen == ["spiffe://jarvis.home/trading/paper"]


def test_gateway_with_another_identity_is_refused(
    ca: WorkloadCa, gateway: _Server, tmp_path: Path
) -> None:
    certs = ca.issue(tmp_path / "client", "trading", "paper")
    with pytest.raises(WorkloadTlsError, match="is not"):
        _post(gateway.port, certs, peer="spiffe://jarvis.home/trading/executor")
    assert gateway.seen == []  # nothing was sent


def test_gateway_from_another_ca_is_refused(gateway: _Server, tmp_path: Path) -> None:
    certs = WorkloadCa("Rogue").issue(tmp_path / "client", "trading", "paper")
    with pytest.raises(WorkloadTlsError, match="handshake"):
        _post(gateway.port, certs)


def test_missing_certificate_fails_before_connecting(tmp_path: Path) -> None:
    with pytest.raises(WorkloadTlsError, match="no usable workload certificate"):
        client_context(tmp_path)


def test_expired_client_certificate_is_refused_by_the_server(
    ca: WorkloadCa, gateway: _Server, tmp_path: Path
) -> None:
    certs = ca.issue(tmp_path / "client", "trading", "paper", expired=True)
    # TLS 1.3 reports the server's rejection after the handshake, on first use.
    with pytest.raises((WorkloadTlsError, ssl.SSLError, ConnectionError)):
        _post(gateway.port, certs)
    assert gateway.seen == []


def test_identities_are_checked() -> None:
    assert spiffe_id("trading", "paper") == "spiffe://jarvis.home/trading/paper"
    for domain, service in [("other", "x"), ("trading", "a/b"), ("trading", "")]:
        with pytest.raises(ValueError):
            spiffe_id(domain, service)


def test_market_data_post_uses_mtls(ca: WorkloadCa, gateway: _Server, tmp_path: Path) -> None:
    certs = ca.issue(tmp_path / "client", "trading", "paper")
    post = workload_post(certs)
    response = post(f"https://localhost:{gateway.port}/mcp/marketdata", b"{}", {})
    assert response.status == 200 and json.loads(response.body) == {"ok": True}
    with pytest.raises(MarketDataError, match="https"):
        post(f"http://localhost:{gateway.port}/mcp/marketdata", b"{}", {})
    rogue = workload_post(certs, peer="spiffe://jarvis.home/trading/executor")
    with pytest.raises(MarketDataError, match="refused"):
        rogue(f"https://localhost:{gateway.port}/mcp/marketdata", b"{}", {})


def test_renewed_certificate_is_picked_up_without_restart(
    ca: WorkloadCa, gateway: _Server, tmp_path: Path
) -> None:
    certs = ca.issue(tmp_path / "client", "trading", "paper", expired=True)
    post = workload_post(certs)
    url = f"https://localhost:{gateway.port}/mcp/marketdata"
    with pytest.raises(MarketDataError):
        post(url, b"{}", {})
    ca.issue(certs, "trading", "paper")  # pki-issue renews in place
    assert post(url, b"{}", {}).status == 200
