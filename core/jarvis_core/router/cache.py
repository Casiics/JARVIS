"""Semantic cache namespacing: one namespace per domain, no caching of sensitive classes."""

from __future__ import annotations

from jarvis_core.contracts.classification import DataClassification, Domain, classify

_UNCACHEABLE = frozenset(
    {DataClassification.CONFIDENTIAL, DataClassification.PRIVATE, DataClassification.SECRET}
)


def cache_namespace(domain: Domain, label: str | DataClassification | None) -> str | None:
    """Namespace for the semantic cache, or None if the request must not be cached."""
    cls = classify(domain, label)
    if domain is Domain.PRIVATE or cls in _UNCACHEABLE:
        return None
    return f"semcache:{domain.value}:{cls.value}"
