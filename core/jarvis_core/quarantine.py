"""Shared helpers for quarantined workers in every domain (docs/architecture.md, 5.2 rule 6).

External content (reels, emails, web pages) reaches a model only wrapped as data. Markers
inside it are neutralised so it cannot close the wrapper and pose as instructions. Answers
come back as one JSON object that a domain's Tier 0b gate validates; injection patterns are
reported as flags that stop automatic follow-ups.
"""

from __future__ import annotations

import json
import re
import unicodedata

_MARKERS = ("<untrusted", "</untrusted")
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)

INJECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "instruction_override": re.compile(
        r"\b(ignore|disregard|forget)\b.{0,40}\b(previous|prior|above|all)\b.{0,40}"
        r"\b(instructions?|rules?|prompts?)\b",
        re.I | re.S,
    ),
    "role_change": re.compile(
        r"\b(you are now|act as|new system prompt)\b|^\s*system\s*:", re.I | re.M
    ),
    "pipe_to_shell": re.compile(r"\b(curl|wget)\b[^\n|]{0,200}\|\s*(sudo\s+)?(ba|z)?sh\b", re.I),
    "destructive_command": re.compile(r"\brm\s+-[a-z]*r[a-z]*f\b|\bmkfs\b|\bdd\s+if=", re.I),
    "credential_request": re.compile(
        r"\b(paste|send|enter|share|dm)\b.{0,40}\b(api[_ -]?key|password|seed phrase|"
        r"private key|token)s?\b|\b(api[_ -]?key|password|seed phrase|private key|token)s?\b"
        r".{0,40}\b(paste|send|enter|share|dm)\b",
        re.I | re.S,
    ),
}


def neutralise(text: str) -> str:
    """Stop external text from opening or closing the untrusted wrapper."""
    out = text.replace("\x00", "")
    for marker in _MARKERS:
        out = out.replace(marker, marker.replace("<", "&lt;"))
        out = out.replace(marker.upper(), marker.upper().replace("<", "&lt;"))
    return out


def wrap(source: str, text: str, limit: int) -> str:
    return f'<untrusted source="{source}">\n{neutralise(text[:limit])}\n</untrusted>'


def parse_json_object(text: str) -> object:
    """The single JSON object in a model answer, ignoring reasoning blocks."""
    cleaned = _THINK.sub("", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object")
    return json.loads(cleaned[start : end + 1])


def injection_flags(text: str) -> tuple[str, ...]:
    return tuple(name for name, pattern in INJECTION_PATTERNS.items() if pattern.search(text))


def has_control_chars(text: str) -> bool:
    return any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in text)
