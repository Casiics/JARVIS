"""Load a domain's curation categories from YAML. Categories are configuration, not code."""

from __future__ import annotations

from pathlib import Path

import yaml

from jarvis_core.curation.models import CurationConfig


def load_config(path: Path) -> CurationConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping")
    return CurationConfig.model_validate(raw)
