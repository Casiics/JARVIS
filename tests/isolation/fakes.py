"""Stand-ins for the internet and for an MCP server, run inside containers by the live tests.

    python fakes.py tls 443 993     TLS servers that answer "ok <FAKE_NAME>" after the handshake
    python fakes.py mcp             JSON-RPC echo on :5678 in place of n8n's MCP endpoint

Standard library only: it runs in the plain Python image.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def serve_tls(port: int, context: ssl.SSLContext, name: str) -> None:
    listener = socket.create_server(("0.0.0.0", port))  # noqa: S104 - test container only
    while True:
        conn, _ = listener.accept()
        try:
            with context.wrap_socket(conn, server_side=True) as tls:
                tls.sendall(f"ok {name}\n".encode())
        except (OSError, ssl.SSLError):
            conn.close()


class McpEcho(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {
                    "echo": request.get("method"),
                    "domain": self.headers.get("x-jarvis-domain"),
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    mode = sys.argv[1]
    if mode == "tls":
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain("/certs/cert.pem", "/certs/key.pem")
        name = os.environ.get("FAKE_NAME", "fake")
        threads = [
            threading.Thread(target=serve_tls, args=(int(p), context, name), daemon=True)
            for p in sys.argv[2:]
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    elif mode == "mcp":
        ThreadingHTTPServer(("0.0.0.0", 5678), McpEcho).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()
