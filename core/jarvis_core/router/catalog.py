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
    # Agent frameworks (Hermes Agent) that send their own large system prompt and tool history.
    AGENT = "agent"


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
    # Hermes Agent asks for at least 64k context. Hermes 3 (Llama 3.1 8B) has a native 128k
    # window; at 64k its KV cache is about 8.6 GB in f16 and about 4.3 GB with Ollama's q8_0
    # cache, next to ~4.7 GB of Q4 weights, so it fits the 12 GB card with
    # OLLAMA_FLASH_ATTENTION=1 and OLLAMA_KV_CACHE_TYPE=q8_0 (estimate, not measured).
    ModelClass.AGENT: LocalModel(name="ollama/hermes3:8b", max_context=65536),
}

# Fallback when a coding prompt exceeds the 14b model's context on 12 GB.
LONG_CONTEXT_CODING = LocalModel(name="ollama/qwen2.5-coder:7b-instruct-q4_K_M", max_context=32768)


def local_context(model: str) -> int:
    """The context window a local model is loaded with.

    Fixed per model on purpose: Ollama reloads a model whenever num_ctx changes, so sizing it
    per call would thrash the single GPU.
    """
    for local in (*LOCAL_MODELS.values(), LONG_CONTEXT_CODING):
        if local.name == model:
            return local.max_context
    return 8192
