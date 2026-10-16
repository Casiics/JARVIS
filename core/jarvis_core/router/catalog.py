"""Model catalog. Local models fit one RTX 4070 Ti (12 GB VRAM), one model loaded at a time.

Cloud model ids are configuration (config/router.yaml), not code.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ModelClass(StrEnum):
    CODING = "coding"
    REASONING = "reasoning"
    VISION = "vision"
    EMBEDDING = "embedding"
    SUPERVISOR = "supervisor"


class Endpoint(StrEnum):
    LOCAL = "local"
    CLOUD = "cloud"


class LocalModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    max_context: int
    on_cpu: bool = False


LOCAL_MODELS: dict[ModelClass, LocalModel] = {
    ModelClass.CODING: LocalModel(
        name="ollama/qwen2.5-coder:14b-instruct-q4_K_M", max_context=8192
    ),
    ModelClass.REASONING: LocalModel(name="ollama/deepseek-r1:8b", max_context=8192),
    ModelClass.VISION: LocalModel(name="ollama/qwen2.5vl:7b", max_context=8192),
    ModelClass.EMBEDDING: LocalModel(name="ollama/bge-m3", max_context=8192, on_cpu=True),
    # The private domain runs its supervisor locally on the reasoning model.
    ModelClass.SUPERVISOR: LocalModel(name="ollama/deepseek-r1:8b", max_context=8192),
}

# Fallback when a coding prompt exceeds the 14b model's context on 12 GB.
LONG_CONTEXT_CODING = LocalModel(name="ollama/qwen2.5-coder:7b-instruct-q4_K_M", max_context=32768)
