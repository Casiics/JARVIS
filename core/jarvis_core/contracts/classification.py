"""Domains and data classification labels."""

from __future__ import annotations

from enum import StrEnum


class Domain(StrEnum):
    """Security domains. Each has its own Hermes instance, n8n instance, stores and network."""

    CORE = "core"
    TRADING = "trading"
    CONTENT = "content"
    PRIVATE = "private"


class DataClassification(StrEnum):
    """Sensitivity label carried by every data object and event."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    PRIVATE = "private"
    SECRET = "secret"  # noqa: S105 - label name, not a credential

    @property
    def rank(self) -> int:
        return _RANK[self]

    def at_least(self, other: DataClassification) -> bool:
        return self.rank >= other.rank


_RANK: dict[DataClassification, int] = {
    DataClassification.PUBLIC: 0,
    DataClassification.INTERNAL: 1,
    DataClassification.CONFIDENTIAL: 2,
    DataClassification.PRIVATE: 3,
    DataClassification.SECRET: 4,
}


def effective_classification(label: str | DataClassification | None) -> DataClassification:
    """Resolve a possibly missing or unknown label. Fails closed: anything unknown is PRIVATE."""
    if isinstance(label, DataClassification):
        return label
    if label is None:
        return DataClassification.PRIVATE
    try:
        return DataClassification(label.strip().lower())
    except ValueError:
        return DataClassification.PRIVATE


def domain_floor(domain: Domain) -> DataClassification:
    """Minimum classification for data originating in a domain."""
    if domain is Domain.PRIVATE:
        return DataClassification.PRIVATE
    return DataClassification.PUBLIC


def classify(domain: Domain, label: str | DataClassification | None) -> DataClassification:
    """Effective classification: the stricter of the label and the origin domain's floor."""
    resolved = effective_classification(label)
    floor = domain_floor(domain)
    return resolved if resolved.at_least(floor) else floor
