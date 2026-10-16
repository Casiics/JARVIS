"""Tier 0b for the reel pipeline: deterministic checks on the quarantined worker's answer.

Errors send the draft back to the worker (no supervisor call). Injection flags do not reject
a draft, but they stop every automatic follow-up: a flagged artifact waits for a human.
Error texts are fixed strings and never quote the draft.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass

from pydantic import ValidationError

from jarvis_core.router.pii import find_pii
from mod_content.reel.schemas import ExtractionDraft, TargetModule

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)

_INJECTION_PATTERNS: dict[str, re.Pattern[str]] = {
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


@dataclass(frozen=True)
class GateReport:
    draft: ExtractionDraft | None
    errors: tuple[str, ...]
    injection_flags: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.draft is not None and not self.errors


def parse_json_object(text: str) -> object:
    cleaned = _THINK.sub("", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object")
    return json.loads(cleaned[start : end + 1])


def check_draft(raw: str) -> GateReport:
    try:
        data = parse_json_object(raw)
    except ValueError:
        return GateReport(None, ("answer is not a single JSON object",))
    try:
        draft = ExtractionDraft.model_validate(data)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) or "root" for e in exc.errors()})
        return GateReport(None, (f"schema violation in: {', '.join(fields)}",))

    errors: list[str] = []
    if draft.utility_score > 0.5 and not draft.actionable_steps:
        errors.append("actionable_steps is empty while utility_score > 0.5")
    if draft.target_module is TargetModule.NONE and draft.utility_score >= 0.5:
        errors.append("target_module is none while utility_score >= 0.5")
    if any(_has_control_chars(t) for t in _texts(draft)):
        errors.append("control characters are not allowed")
    if len(set(draft.actionable_steps)) != len(draft.actionable_steps):
        errors.append("actionable_steps contains duplicates")
    if errors:
        return GateReport(None, tuple(errors))
    return GateReport(draft, (), injection_flags(draft))


def injection_flags(draft: ExtractionDraft) -> tuple[str, ...]:
    text = "\n".join(_texts(draft))
    flags = [name for name, pattern in _INJECTION_PATTERNS.items() if pattern.search(text)]
    flags.extend(f"pii:{hit}" for hit in find_pii(text))
    return tuple(flags)


def _texts(draft: ExtractionDraft) -> list[str]:
    return [draft.summary, *draft.extracted_tools, *draft.actionable_steps]


def _has_control_chars(text: str) -> bool:
    return any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in text)
