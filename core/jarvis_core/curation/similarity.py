"""Deterministic text similarity for duplicate detection. No model calls, no network."""

from __future__ import annotations

import hashlib
import re
import unicodedata

_WORD = re.compile(r"[a-z0-9äöüß+#.-]{2,}")

# Small bilingual stop list: the material is mostly English and German.
STOP_WORDS = frozenset(
    """
    the a an and or of to in on for with is are was be it this that you your how what why
    use using from as at by can will just more most not no do does into about than then them
    der die das und oder ein eine zu im in mit ist sind für auf von wie was du dein den dem des
    nicht auch noch nur so man sich es als bei aus
    """.split()
)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(text.split())


def tokens(text: str) -> frozenset[str]:
    words = (w.strip(".-") for w in _WORD.findall(normalize(text)))
    return frozenset(w for w in words if len(w) > 1 and w not in STOP_WORDS)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def overlap(a: frozenset[str], b: frozenset[str]) -> float:
    """Share of the smaller set found in the larger one: catches a short text inside a long one."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """Blend of Jaccard and containment, so paraphrased re-posts still match."""
    return round(0.5 * jaccard(a, b) + 0.5 * overlap(a, b), 4)


def fingerprint(*parts: str) -> str:
    """Hash of the normalised text; equal hashes mean exact duplicates."""
    joined = "\x1f".join(normalize(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def unique(values: list[str] | tuple[str, ...], limit: int) -> tuple[str, ...]:
    """Order-preserving de-duplication by normalised form, capped at `limit`."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = normalize(value)
        if key and key not in seen:
            seen.add(key)
            out.append(value.strip())
        if len(out) >= limit:
            break
    return tuple(out)
