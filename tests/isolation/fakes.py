"""Stand-ins for the internet and for an MCP server, run inside containers by the live tests.

    python fakes.py tls 443 993     TLS servers that answer "ok <FAKE_NAME>" after the handshake
    python fakes.py mcp             JSON-RPC echo on :5678 in place of n8n's MCP endpoint
    python fakes.py caldav          CalDAV REPORT on :443 (TLS), one event today
    python fakes.py imap            IMAP4rev1 on :993 (TLS), two messages, read-only commands
    python fakes.py ollama          /api/chat on :11434 with canned answers per output schema

The CalDAV and IMAP fakes check the credentials in FAKE_USER and FAKE_PASSWORD. The CalDAV,
IMAP and Ollama fakes log one JSON line per request to stdout for the tests to inspect.

Standard library only: it runs in the plain Python image.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import sys
import threading
from datetime import UTC, datetime, timedelta
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


def log(**fields: object) -> None:
    print(json.dumps(fields), flush=True)


def tls_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/certs/cert.pem", "/certs/key.pem")
    return context


def calendar_payload() -> bytes:
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
    stamp = "%Y%m%dT%H%M%SZ"
    ics = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:live-event-1\r\n"
        f"DTSTART:{start.strftime(stamp)}\r\nDTEND:{(start + timedelta(hours=1)).strftime(stamp)}"
        "\r\nSUMMARY:Zahnarzt\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:response><d:href>/cal/live-event-1.ics</d:href><d:propstat><d:prop>"
        f"<c:calendar-data>{ics}</c:calendar-data>"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        "</d:multistatus>"
    ).encode()


class CalDav(BaseHTTPRequestHandler):
    def do_REPORT(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        expected = (
            "Basic "
            + base64.b64encode(
                f"{os.environ['FAKE_USER']}:{os.environ['FAKE_PASSWORD']}".encode()
            ).decode()
        )
        authorized = self.headers.get("authorization") == expected
        log(method="REPORT", path=self.path, depth=self.headers.get("depth"), auth=authorized)
        body = calendar_payload() if authorized else b""
        self.send_response(207 if authorized else 401)
        self.send_header("content-type", "application/xml; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


MAILS = {
    1: (
        b"From: Anna <anna@example.org>\r\nTo: owner@example.org\r\nSubject: Termin\r\n"
        b"Date: Thu, 15 Oct 2026 09:00:00 +0200\r\nMessage-ID: <1@example.org>\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\nPasst dir Donnerstag?\r\n"
    ),
    2: (
        b"From: Shop <news@shop.example>\r\nTo: owner@example.org\r\nSubject: Angebote\r\n"
        b"Date: Thu, 15 Oct 2026 10:00:00 +0200\r\nMessage-ID: <2@shop.example>\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\nNur heute.\r\n"
    ),
}


def imap_session(conn: ssl.SSLSocket) -> None:
    reader = conn.makefile("rb")

    def send(line: str | bytes) -> None:
        conn.sendall((line.encode() if isinstance(line, str) else line) + b"\r\n")

    send("* OK [CAPABILITY IMAP4rev1 AUTH=PLAIN] fake ready")
    while line := reader.readline():
        tag, _, rest = line.decode().strip().partition(" ")
        command, _, args = rest.partition(" ")
        command = command.upper()
        if command == "UID":
            sub, _, args = args.partition(" ")
            command = f"UID {sub.upper()}"
        log(command=command)
        if command == "CAPABILITY":
            send("* CAPABILITY IMAP4rev1 AUTH=PLAIN")
        elif command == "LOGIN":
            user, password = (p.strip('"') for p in args.split(" ", 1))
            if (user, password) != (os.environ["FAKE_USER"], os.environ["FAKE_PASSWORD"]):
                send(f"{tag} NO login failed")
                continue
        elif command == "EXAMINE":
            send(f"* {len(MAILS)} EXISTS")
            send("* OK [UIDVALIDITY 42] UIDs valid")
            send(f"{tag} OK [READ-ONLY] EXAMINE completed")
            continue
        elif command == "UID SEARCH":
            send("* SEARCH " + " ".join(str(uid) for uid in MAILS))
        elif command == "UID FETCH":
            uid_text, _, what = args.partition(" ")
            raw = MAILS[int(uid_text)]
            if "RFC822.SIZE" in what:
                send(f"* {uid_text} FETCH (UID {uid_text} RFC822.SIZE {len(raw)})")
            else:
                send(f"* {uid_text} FETCH (UID {uid_text} BODY[] {{{len(raw)}}}".encode())
                conn.sendall(raw)
                send(")")
        elif command == "LOGOUT":
            send("* BYE")
            send(f"{tag} OK LOGOUT completed")
            return
        else:
            send(f"{tag} NO {command} is not offered by this fake")
            continue
        send(f"{tag} OK {command} completed")


def serve_imap(context: ssl.SSLContext) -> None:
    listener = socket.create_server(("0.0.0.0", 993))  # noqa: S104 - test container only
    while True:
        raw, _ = listener.accept()
        try:
            with context.wrap_socket(raw, server_side=True) as conn:
                imap_session(conn)
        except (OSError, ssl.SSLError):
            raw.close()


def canned(schema: dict[str, object]) -> dict[str, object]:
    properties = schema.get("properties")
    keys = set(properties) if isinstance(properties, dict) else set()
    if "category" in keys:
        return {"category": "fyi", "summary": "Eine Nachricht ohne Handlungsbedarf."}
    if "headline" in keys:
        return {"headline": "Ein ruhiger Tag mit einem Termin.", "items": []}
    if "accept" in keys:
        return {"accept": True, "feedback": []}
    return {"body": "Donnerstag passt mir gut."}


class Ollama(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length)
        request = json.loads(raw or b"{}")
        schema = request.get("format") if isinstance(request.get("format"), dict) else {}
        log(path=self.path, model=request.get("model"), request=raw.decode())
        answer = canned(schema)
        body = json.dumps(
            {
                "message": {"role": "assistant", "content": json.dumps(answer)},
                "prompt_eval_count": 10,
                "eval_count": 5,
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        body = b'{"models": []}'
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
    elif mode == "caldav":
        server = ThreadingHTTPServer(("0.0.0.0", 443), CalDav)  # noqa: S104
        server.socket = tls_context().wrap_socket(server.socket, server_side=True)
        server.serve_forever()
    elif mode == "imap":
        serve_imap(tls_context())
    elif mode == "ollama":
        ThreadingHTTPServer(("0.0.0.0", 11434), Ollama).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()
