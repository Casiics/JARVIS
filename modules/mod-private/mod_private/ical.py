"""A small iCalendar (RFC 5545) reader for VEVENTs: enough for a daily overview.

Handles line folding, parameters, text escapes, UTC/TZID/floating date-times and all-day
dates. Recurrence rules are not expanded; recurring events are marked so the briefing can
say so. Unknown time zones and malformed events are skipped, never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from mod_private.schemas import CalendarEvent

MAX_LINES = 50_000


@dataclass(frozen=True)
class Property:
    name: str
    params: dict[str, str]
    value: str


def unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
        if len(lines) > MAX_LINES:
            raise ValueError("calendar data too large")
    return lines


def parse_line(line: str) -> Property | None:
    head, sep, value = line.partition(":")
    if not sep:
        return None
    name, *raw_params = head.split(";")
    params: dict[str, str] = {}
    for item in raw_params:
        key, _, val = item.partition("=")
        params[key.upper()] = val.strip('"')
    return Property(name.upper(), params, value)


def unescape(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        char = value[i]
        if char == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


def parse_time(prop: Property, default_tz: tzinfo) -> datetime | date:
    value = prop.value.strip()
    if prop.params.get("VALUE") == "DATE" or len(value) == 8:
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    utc = value.endswith("Z")
    stamp = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    if utc:
        return stamp.replace(tzinfo=UTC)
    tzid = prop.params.get("TZID")
    zone: tzinfo = ZoneInfo(tzid) if tzid else default_tz
    return stamp.replace(tzinfo=zone)


def parse_events(text: str, default_tz: tzinfo = UTC) -> list[CalendarEvent]:
    events: list[CalendarEvent] = []
    current: dict[str, Property] | None = None
    depth = 0
    for line in unfold(text):
        prop = parse_line(line)
        if prop is None:
            continue
        if prop.name == "BEGIN" and prop.value.upper() == "VEVENT":
            current, depth = {}, 0
        elif current is not None and prop.name == "BEGIN":
            depth += 1  # VALARM and friends: their properties are not the event's
        elif current is not None and prop.name == "END" and prop.value.upper() != "VEVENT":
            depth -= 1
        elif current is not None and prop.name == "END":
            event = _build(current, default_tz)
            if event is not None:
                events.append(event)
            current = None
        elif current is not None and depth == 0:
            current.setdefault(prop.name, prop)
    return events


def _build(props: dict[str, Property], default_tz: tzinfo) -> CalendarEvent | None:
    if "UID" not in props or "DTSTART" not in props:
        return None
    try:
        start = parse_time(props["DTSTART"], default_tz)
        end = parse_time(props["DTEND"], default_tz) if "DTEND" in props else None
        return CalendarEvent(
            uid=props["UID"].value[:500],
            summary=unescape(props["SUMMARY"].value)[:500] if "SUMMARY" in props else "",
            start=start,
            end=end,
            location=unescape(props["LOCATION"].value)[:500] if "LOCATION" in props else "",
            description=(
                unescape(props["DESCRIPTION"].value)[:4000] if "DESCRIPTION" in props else ""
            ),
            recurring="RRULE" in props or "RDATE" in props,
        )
    except (ValueError, ZoneInfoNotFoundError, ValidationError):
        return None
