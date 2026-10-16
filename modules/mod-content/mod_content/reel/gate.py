"""Tier 0b for the reel pipeline: deterministic checks on the quarantined worker's answer.

Errors send the draft back to the worker (no supervisor call). Injection flags do not reject
a draft, but they stop every automatic follow-up: a flagged artifact waits for a human.
Error texts are fixed strings and never quote the draft.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from jarvis_core.quarantine import has_control_chars, parse_json_object
from jarvis_core.quarantine import injection_flags as pattern_flags
from jarvis_core.router.pii import find_pii
from mod_content.reel.schemas import ExtractionDraft, TargetModule


@dataclass(frozen=True)
class GateReport:
    draft: ExtractionDraft | None
    errors: tuple[str, ...]
    injection_flags: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.draft is not None and not self.errors


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
    if any(has_control_chars(t) for t in _texts(draft)):
        errors.append("control characters are not allowed")
    if len(set(draft.actionable_steps)) != len(draft.actionable_steps):
        errors.append("actionable_steps contains duplicates")
    if errors:
        return GateReport(None, tuple(errors))
    return GateReport(draft, (), injection_flags(draft))


def injection_flags(draft: ExtractionDraft) -> tuple[str, ...]:
    text = "\n".join(_texts(draft))
    flags = list(pattern_flags(text))
    flags.extend(f"pii:{hit}" for hit in find_pii(text))
    return tuple(flags)


def _texts(draft: ExtractionDraft) -> list[str]:
    return [draft.summary, *draft.extracted_tools, *draft.actionable_steps]
