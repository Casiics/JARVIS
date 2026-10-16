"""Model clients behind the router. Only the inference gateway calls them.

The Ollama client talks to the single Ollama instance in `net-inference`. A cloud client
(LiteLLM) implements the same protocol once cloud keys exist.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class ModelClientError(Exception):
    """The model endpoint failed or returned something unusable."""


class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str
    images_b64: tuple[str, ...] = ()


class ModelCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    messages: tuple[ChatMessage, ...]
    json_schema: dict[str, Any] | None = None
    temperature: float = Field(default=0.0, ge=0, le=2)
    max_output_tokens: int = Field(default=1024, gt=0, le=8192)
    context_tokens: int = Field(default=8192, gt=0)


class ModelResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)


class ModelClient(Protocol):
    def complete(self, call: ModelCall) -> ModelResult: ...

    def loaded_models(self) -> list[str]: ...

    def unload(self, model: str) -> None: ...


def _ollama_name(model: str) -> str:
    return model.removeprefix("ollama/")


class OllamaClient:
    """Minimal Ollama HTTP client (stdlib only). Tools are never passed to the model."""

    def __init__(self, base_url: str = "http://ollama:11434", timeout_s: float = 300.0) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be http(s)")
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s

    def complete(self, call: ModelCall) -> ModelResult:
        messages: list[dict[str, Any]] = []
        for m in call.messages:
            item: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.images_b64:
                item["images"] = list(m.images_b64)
            messages.append(item)
        body: dict[str, Any] = {
            "model": _ollama_name(call.model),
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": call.temperature,
                "num_predict": call.max_output_tokens,
                "num_ctx": call.context_tokens,
            },
        }
        if call.json_schema is not None:
            body["format"] = call.json_schema
        data = self._post("/api/chat", body)
        message = data.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ModelClientError("ollama response has no message content")
        return ModelResult(
            text=message["content"],
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            completion_tokens=int(data.get("eval_count") or 0),
        )

    def loaded_models(self) -> list[str]:
        data = self._request("GET", "/api/ps", None)
        models = data.get("models") or []
        return [f"ollama/{m['name']}" for m in models if isinstance(m, dict) and "name" in m]

    def unload(self, model: str) -> None:
        self._post("/api/generate", {"model": _ollama_name(model), "keep_alive": 0})

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, body)

    def _request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        payload = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(  # noqa: S310 - base_url is checked to be http(s)
            self._base + path,
            data=payload,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
                parsed = json.loads(resp.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise ModelClientError(f"ollama {path} failed: {type(exc).__name__}") from exc
        if not isinstance(parsed, dict):
            raise ModelClientError(f"ollama {path} returned non-object JSON")
        return parsed
