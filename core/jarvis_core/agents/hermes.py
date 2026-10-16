"""Hermes Agent (Nous Research) as a domain runtime, held to the inference gateway's policy.

Hermes Agent normally calls its model provider itself with its own API key. That would bypass
the routing policy, the FinOps circuit breaker and the audit, and put a key into the agent's
environment. Here it does neither:

- For each turn a `ScopedModelEndpoint` starts on loopback: a minimal OpenAI-compatible
  `/v1/chat/completions` server bound to one task, one domain and one label. Every request it
  receives goes through `InferenceGateway.complete`, so private stays local, budgets apply and
  every call is audited. Tool definitions Hermes sends are dropped; the model never sees them.
- Hermes runs with a throw-away home directory whose `config.yaml` names that endpoint as its
  only provider (`provider: custom`), with an empty CLI toolset list, and with a scrubbed
  environment (no keys, no inherited variables). The bearer key is random per turn and only
  valid for that endpoint.
- In production the command is prefixed with the sandbox runner (read-only root, no network
  but the endpoint), set through `command`.

Hermes Agent wants a large context window (64k according to its quickstart); the local
models in decision 4 run at 8k. Until a larger GPU or a cloud route exists for a domain,
`GatewayRuntime` stays the default and this runtime is opt-in per domain.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from jarvis_core.agents.runtime import (
    AgentReply,
    AgentRuntimeError,
    AgentTurn,
    TurnScope,
    check_scope,
)
from jarvis_core.contracts.classification import Domain
from jarvis_core.router.catalog import ModelClass
from jarvis_core.router.client import ChatMessage, ModelClientError
from jarvis_core.router.gateway import InferenceGateway
from jarvis_core.router.policy import RoutingDenied

MAX_REQUEST_BYTES = 1024 * 1024
MODEL_ALIAS = "jarvis"


class _ChatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")  # tools, tool_choice, ... are dropped on purpose

    messages: list[dict[str, Any]]
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    stream: bool = False


def _text(content: Any) -> str:
    """OpenAI content is a string or a list of parts; only text parts are kept."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(p.get("text", ""))
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def _messages(raw: list[dict[str, Any]]) -> tuple[ChatMessage, ...]:
    out: list[ChatMessage] = []
    for item in raw:
        role = item.get("role")
        text = _text(item.get("content"))
        if role in ("system", "developer"):
            out.append(ChatMessage(role="system", content=text))
        elif role == "assistant":
            out.append(ChatMessage(role="assistant", content=text))
        elif role == "tool":
            # Tools are never offered, so a tool message can only be replayed history; it is data.
            out.append(ChatMessage(role="user", content=f"(tool output)\n{text}"))
        else:
            out.append(ChatMessage(role="user", content=text))
    if not out:
        raise ValueError("no messages")
    return tuple(out)


@dataclass
class _Usage:
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_eur: float = 0.0
    calls: int = 0
    error_class: str | None = None


@dataclass
class ScopedModelEndpoint:
    """A loopback OpenAI-compatible endpoint for exactly one task turn."""

    gateway: InferenceGateway
    domain: Domain
    scope: TurnScope
    model_class: ModelClass
    require_local: bool = False
    max_calls: int = 12
    api_key: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    usage: _Usage = field(default_factory=_Usage)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def complete(self, request: _ChatRequest) -> str:
        with self._lock:
            if self.usage.calls >= self.max_calls:
                raise RoutingDenied("model call limit for this turn reached")
            self.usage.calls += 1
        limit = request.max_completion_tokens or request.max_tokens or 2048
        result = self.gateway.complete(
            caller_domain=self.domain,
            label=self.scope.label,
            model_class=self.model_class,
            messages=_messages(request.messages),
            task_id=self.scope.task_id,
            trace_id=self.scope.trace_id,
            taint=self.scope.taint,
            require_local=self.require_local or self.domain is Domain.PRIVATE,
            max_output_tokens=max(1, min(limit, 8192)),
        )
        with self._lock:
            self.usage.model = result.route.model[:100]
            self.usage.prompt_tokens += result.prompt_tokens
            self.usage.completion_tokens += result.completion_tokens
            self.usage.cost_eur += result.cost_eur
        return result.text

    @contextmanager
    def serve(self) -> Iterator[str]:
        """Run the endpoint on 127.0.0.1 and yield its base URL (ending in /v1)."""
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return  # no request logging: paths and bodies stay out of logs

            def _authorised(self) -> bool:
                header = self.headers.get("Authorization", "")
                return secrets.compare_digest(header, f"Bearer {endpoint.api_key}")

            def _json(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if not self._authorised():
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": {"message": "unauthorised"}})
                elif self.path.rstrip("/") == "/v1/models":
                    self._json(
                        HTTPStatus.OK,
                        {"object": "list", "data": [{"id": MODEL_ALIAS, "object": "model"}]},
                    )
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})

            def do_POST(self) -> None:
                if not self._authorised():
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": {"message": "unauthorised"}})
                    return
                if self.path.rstrip("/") != "/v1/chat/completions":
                    self._json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > MAX_REQUEST_BYTES:
                    self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": {"message": "size"}})
                    return
                try:
                    request = _ChatRequest.model_validate_json(self.rfile.read(length))
                    text = endpoint.complete(request)
                except (ValidationError, ValueError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": {"message": "bad request"}})
                    return
                except RoutingDenied:
                    endpoint.usage.error_class = "routing_denied"
                    self._json(HTTPStatus.FORBIDDEN, {"error": {"message": "denied by policy"}})
                    return
                except ModelClientError:
                    endpoint.usage.error_class = "model_unavailable"
                    self._json(HTTPStatus.BAD_GATEWAY, {"error": {"message": "model failed"}})
                    return
                if request.stream:
                    self._stream(text)
                else:
                    self._json(HTTPStatus.OK, _completion(text, endpoint.usage))

            def _stream(self, text: str) -> None:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for chunk in _stream_chunks(text):
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}/v1"
        finally:
            server.shutdown()
            server.server_close()


def _completion(text: str, usage: _Usage) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{secrets.token_hex(8)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_ALIAS,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.prompt_tokens + usage.completion_tokens,
        },
    }


def _stream_chunks(text: str) -> list[dict[str, Any]]:
    base = {
        "id": f"chatcmpl-{secrets.token_hex(8)}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_ALIAS,
    }
    return [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]


def hermes_config(base_url: str, api_key: str) -> str:
    """Hermes `config.yaml`: the scoped endpoint as the only provider, no CLI toolsets."""
    return (
        "model:\n"
        f"  default: {MODEL_ALIAS}\n"
        "  provider: custom\n"
        f"  base_url: {base_url}\n"
        f"  api_key: {api_key}\n"
        "platform_toolsets:\n"
        "  cli: []\n"
    )


@dataclass
class HermesRuntime:
    """Runs one Hermes Agent one-shot per turn (`hermes -z`), model calls via the gateway."""

    gateway: InferenceGateway
    domain: Domain
    command: Sequence[str] = ("hermes",)
    timeout_s: float = 600.0
    require_local: bool = False
    max_calls_per_turn: int = 12

    def run(self, turn: AgentTurn, scope: TurnScope) -> AgentReply:
        check_scope(self, scope)
        endpoint = ScopedModelEndpoint(
            gateway=self.gateway,
            domain=self.domain,
            scope=scope,
            model_class=turn.model_class,
            require_local=self.require_local,
            max_calls=self.max_calls_per_turn,
        )
        prompt = f"{turn.system}\n\n{turn.prompt}"
        if turn.json_schema is not None:
            prompt += "\n\nReply with one JSON object only."
        with endpoint.serve() as base_url, tempfile.TemporaryDirectory() as home:
            config_dir = Path(home) / ".hermes"
            config_dir.mkdir(mode=0o700)
            (config_dir / "config.yaml").write_text(
                hermes_config(base_url, endpoint.api_key), encoding="utf-8"
            )
            env = {
                "HOME": home,
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": "C.UTF-8",
                "NO_COLOR": "1",
            }
            try:
                done = subprocess.run(  # noqa: S603 - fixed argv, no shell
                    [*self.command, "-z", prompt],
                    env=env,
                    cwd=home,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_s,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise AgentRuntimeError("hermes_missing") from exc
            except subprocess.TimeoutExpired as exc:
                raise AgentRuntimeError("hermes_timeout") from exc
        usage = endpoint.usage
        if usage.error_class is not None:
            raise AgentRuntimeError(usage.error_class)
        if done.returncode != 0 or usage.calls == 0:
            raise AgentRuntimeError("hermes_failed")
        return AgentReply(
            text=done.stdout.strip(),
            model=usage.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_eur=usage.cost_eur,
        )
