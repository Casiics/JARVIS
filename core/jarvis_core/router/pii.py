"""Conservative PII and secret detector for cloud-bound prompts. A hit blocks, never redacts."""

from __future__ import annotations

import re

_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "iban": re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,4})?\b"),
    "phone": re.compile(r"(?<!\w)\+?\d[\d\s/()-]{8,}\d(?!\w)"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "api_key": re.compile(r"\b(?:sk|pk|ghp|gho|github_pat|xox[abp])[-_][A-Za-z0-9_-]{16,}\b"),
}


def find_pii(text: str) -> list[str]:
    """Names of the patterns found in text."""
    return [name for name, pattern in _PATTERNS.items() if pattern.search(text)]
