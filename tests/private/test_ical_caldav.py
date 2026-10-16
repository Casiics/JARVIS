from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from mod_private.caldav import CalDAVClient, CalDAVError, https_transport, parse_multistatus
from mod_private.ical import parse_events

BERLIN = ZoneInfo("Europe/Berlin")

ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
    "BEGIN:VEVENT\r\nUID:a@x\r\nSUMMARY:Dentist\\, check-up\r\n"
    "DTSTART;TZID=Europe/Berlin:20261016T093000\r\nDTEND;TZID=Europe/Berlin:20261016T100000\r\n"
    "LOCATION:Main St\r\n 12\r\n"
    "BEGIN:VALARM\r\nDESCRIPTION:alarm text\r\nEND:VALARM\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:b@x\r\nSUMMARY:Holiday\r\nDTSTART;VALUE=DATE:20261016\r\n"
    "DTEND;VALUE=DATE:20261017\r\nRRULE:FREQ=YEARLY\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:c@x\r\nDTSTART:20261016T150000Z\r\nSUMMARY:Call\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nSUMMARY:no uid\r\nDTSTART:20261016T150000Z\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:d@x\r\nDTSTART;TZID=Mars/Olympus:20261016T150000\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def test_parse_events() -> None:
    events = {e.uid: e for e in parse_events(ICS, BERLIN)}
    assert set(events) == {"a@x", "b@x", "c@x"}
    dentist = events["a@x"]
    assert dentist.summary == "Dentist, check-up"
    assert dentist.location == "Main St12"
    assert dentist.description == ""  # the VALARM's description is not the event's
    assert dentist.start == datetime(2026, 10, 16, 9, 30, tzinfo=BERLIN)
    assert events["b@x"].start == date(2026, 10, 16) and events["b@x"].all_day
    assert events["b@x"].recurring
    assert events["c@x"].start == datetime(2026, 10, 16, 15, 0, tzinfo=UTC)


def multistatus(*bodies: str) -> bytes:
    responses = "".join(
        "<d:response><d:href>/c/1.ics</d:href><d:propstat><d:prop>"
        f"<c:calendar-data>{b}</c:calendar-data></d:prop></d:propstat></d:response>"
        for b in bodies
    )
    return (
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
        f'xmlns:c="urn:ietf:params:xml:ns:caldav">{responses}</d:multistatus>'
    ).encode()


def test_client_sends_one_report_and_parses() -> None:
    seen: list[tuple[str, dict[str, str], bytes]] = []

    def transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        seen.append((url, headers, body))
        return 207, multistatus(ICS.replace("&", "&amp;"))

    client = CalDAVClient("https://cal.example.org/u/cal/", "u", "pw", BERLIN, transport)
    start = datetime(2026, 10, 15, tzinfo=UTC)
    events = client.events(start, datetime(2026, 10, 30, tzinfo=UTC))
    assert [e.uid for e in events] == ["b@x", "a@x", "c@x"]
    [(_url, headers, body)] = seen
    assert headers["Authorization"] == "Basic dTpwdw=="
    assert headers["Depth"] == "1"
    assert b'start="20261015T000000Z" end="20261030T000000Z"' in body
    assert "pw" not in repr(client)


def test_client_raises_on_error_status() -> None:
    client = CalDAVClient("https://x/", "u", "p", transport=lambda u, h, b: (401, b""))
    with pytest.raises(CalDAVError):
        client.events(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC))


def test_multistatus_refuses_dtds_and_garbage() -> None:
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'
    with pytest.raises(CalDAVError):
        parse_multistatus(evil)
    with pytest.raises(CalDAVError):
        parse_multistatus(b"<not xml")


def test_transport_is_https_only() -> None:
    with pytest.raises(CalDAVError):
        https_transport("http://cal.example.org/", {}, b"")
