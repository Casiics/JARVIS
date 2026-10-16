"""Read-only CalDAV client: one calendar-query REPORT for a time range.

Runs in the private domain's executor, which holds the CalDAV credentials; models never see
them. The server is reached through egress-private, with TLS verified end to end.
"""

from __future__ import annotations

import base64
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from typing import Protocol
from xml.etree import ElementTree as ET

from mod_private.ical import parse_events
from mod_private.schemas import CalendarEvent

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}

REPORT_BODY = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><c:calendar-data/></d:prop>
  <c:filter>
    <c:comp-filter name="VCALENDAR">
      <c:comp-filter name="VEVENT">
        <c:time-range start="{start}" end="{end}"/>
      </c:comp-filter>
    </c:comp-filter>
  </c:filter>
</c:calendar-query>
"""


class CalDAVError(Exception):
    pass


class Transport(Protocol):
    def __call__(self, url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]: ...


def https_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
    if not url.startswith("https://"):
        raise CalDAVError("CalDAV must use https")
    request = urllib.request.Request(url, data=body, method="REPORT", headers=headers)  # noqa: S310
    context = ssl.create_default_context()
    # No proxy: the host name resolves to egress-private, which passes TLS through.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    )
    try:
        with opener.open(request, timeout=30) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
            return response.status, data
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def parse_multistatus(payload: bytes) -> list[str]:
    """calendar-data texts from a 207 response. Refuses DTDs (entity expansion)."""
    if len(payload) > MAX_RESPONSE_BYTES:
        raise CalDAVError("response too large")
    head = payload[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in payload.lower():
        raise CalDAVError("DTDs are not accepted")
    try:
        root = ET.fromstring(payload)  # noqa: S314 - DTDs refused above
    except ET.ParseError as exc:
        raise CalDAVError("malformed multistatus") from exc
    return [node.text for node in root.iterfind(".//c:calendar-data", NS) if node.text]


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


@dataclass
class CalDAVClient:
    calendar_url: str
    username: str
    password: str = field(repr=False)
    default_tz: tzinfo = UTC
    transport: Transport = https_transport
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        if end <= start:
            raise ValueError("end must be after start")
        credentials = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        headers = {
            "Authorization": f"Basic {credentials}",
            "Depth": "1",
            "Content-Type": "application/xml; charset=utf-8",
        }
        body = REPORT_BODY.format(start=_stamp(start), end=_stamp(end)).encode()
        status, payload = self.transport(self.calendar_url, headers, body)
        if status != 207:
            raise CalDAVError(f"calendar-query answered {status}")
        events: list[CalendarEvent] = []
        for text in parse_multistatus(payload):
            events.extend(parse_events(text, self.default_tz))
        return sorted(events, key=_sort_key)


def _sort_key(event: CalendarEvent) -> datetime:
    start = event.start
    if isinstance(start, datetime):
        return start
    return datetime(start.year, start.month, start.day, tzinfo=UTC)
