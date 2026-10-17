"""Network probes run inside a container at one position in the network, by the live tests.

Each command prints one JSON object:
    tcp HOST PORT                          {"ok": bool, "error": str}
    connect PROXY_HOST PROXY_PORT HOST PORT  {"status": int}  (HTTP CONNECT through a proxy)
    serial HOST PORT                       {"serial": str}  (of the server's certificate)
    tls HOST PORT SNI                      {"ok": bool, "banner": str, "error": str}
                                           (ok after the handshake; banner if the server talks)
    http METHOD URL [JSON]                 {"status": int, "body": str}
    mcp URL METHOD PARAMS_JSON             {"status": int, "body": str}  (after an MCP handshake)

https URLs use mutual TLS with the workload certificate in $PROBE_CERTS (tls.crt, tls.key,
root_ca.crt; default /certs/ok). PROBE_CERTS=none connects without a client certificate.

Standard library only: it runs in the plain Python image.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request
from typing import Any


def tcp(host: str, port: int) -> dict[str, Any]:
    try:
        socket.create_connection((host, port), timeout=3).close()
    except OSError as exc:
        return {"ok": False, "error": type(exc).__name__}
    return {"ok": True, "error": ""}


def connect(proxy_host: str, proxy_port: int, host: str, port: int) -> dict[str, Any]:
    with socket.create_connection((proxy_host, proxy_port), timeout=5) as sock:
        sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
        status_line = sock.recv(1024).split(b"\r\n", 1)[0].decode()
    return {"status": int(status_line.split()[1])}


def tls(host: str, port: int, sni: str) -> dict[str, Any]:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # fakes use a throwaway certificate
    try:
        with socket.create_connection((host, port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=sni) as conn:
                conn.settimeout(2)
                try:
                    banner = conn.recv(256).decode().strip()
                except TimeoutError:
                    banner = ""  # handshake done; server waits for the client (HTTP)
    except (OSError, ssl.SSLError) as exc:
        return {"ok": False, "banner": "", "error": type(exc).__name__}
    return {"ok": True, "banner": banner, "error": ""}


def _workload_context() -> ssl.SSLContext:
    certs = os.environ.get("PROBE_CERTS", "/certs/ok")
    if certs == "none":
        return ssl.create_default_context(cafile="/certs/ok/root_ca.crt")
    context = ssl.create_default_context(cafile=f"{certs}/root_ca.crt")
    context.load_cert_chain(f"{certs}/tls.crt", f"{certs}/tls.key")
    return context


def serial(host: str, port: int) -> dict[str, Any]:
    """The serial number of the certificate a workload server presents, verified."""
    context = _workload_context()
    try:
        with socket.create_connection((host, port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=host) as conn:
                cert = conn.getpeercert() or {}
    except (OSError, ssl.SSLError):
        return {"serial": ""}
    return {"serial": str(cert.get("serialNumber", ""))}


def http(method: str, url: str, payload: str | None, session: str | None = None) -> dict[str, Any]:
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    if session:
        headers["mcp-session-id"] = session
    request = urllib.request.Request(  # noqa: S310 - test probe
        url,
        data=payload.encode() if payload is not None else None,
        method=method,
        headers=headers,
    )
    try:
        context = _workload_context() if url.startswith("https://") else None
        with urllib.request.urlopen(request, timeout=20, context=context) as response:  # noqa: S310
            return {
                "status": response.status,
                "body": response.read().decode(),
                "session": response.headers.get("mcp-session-id"),
            }
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "body": exc.read().decode()}
    except (OSError, ssl.SSLError) as exc:
        reason = getattr(exc, "reason", None)
        name = type(reason).__name__ if isinstance(reason, BaseException) else type(exc).__name__
        return {"status": 0, "body": name}


def mcp(url: str, method: str, params: str) -> dict[str, Any]:
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "jarvis-probe", "version": "0"},
        },
    }
    opened = http("POST", url, json.dumps(init))
    if opened["status"] != 200:
        return opened
    session = opened.get("session")
    done = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    http("POST", url, json.dumps(done), session)
    call = {"jsonrpc": "2.0", "id": 2, "method": method, "params": json.loads(params)}
    result = http("POST", url, json.dumps(call), session)
    result.pop("session", None)
    return result


def main() -> None:
    cmd, *args = sys.argv[1:]
    if cmd == "tcp":
        result = tcp(args[0], int(args[1]))
    elif cmd == "connect":
        result = connect(args[0], int(args[1]), args[2], int(args[3]))
    elif cmd == "serial":
        result = serial(args[0], int(args[1]))
    elif cmd == "tls":
        result = tls(args[0], int(args[1]), args[2])
    elif cmd == "mcp":
        result = mcp(args[0], args[1], args[2])
    elif cmd == "http":
        result = http(args[0], args[1], args[2] if len(args) > 2 else None)
    else:
        raise SystemExit(f"unknown command {cmd}")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
