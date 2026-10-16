"""The smoke command runs one full loop on whatever answers as the local model."""

from __future__ import annotations

import json

import pytest

from jarvis_core.agents import __main__ as cli
from jarvis_core.router.client import ModelCall, ModelResult

SUMMARY = '{"title": "JARVIS domains", "points": ["four domains", "private stays local"]}'


class Answers:
    def __init__(self, _url: str) -> None:
        self.calls: list[ModelCall] = []

    def complete(self, call: ModelCall) -> ModelResult:
        self.calls.append(call)
        supervisor = "supervisor" in call.messages[0].content
        text = '{"accept": true}' if supervisor else SUMMARY
        return ModelResult(text=text, prompt_tokens=10, completion_tokens=5)

    def loaded_models(self) -> list[str]:
        return []

    def unload(self, model: str) -> None:
        return None


def test_smoke_runs_worker_gate_and_supervisor_locally(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "OllamaClient", Answers)
    assert cli.main(["smoke"]) == 0
    out = capsys.readouterr().out
    report = json.loads(out[out.index("{") :])
    assert report["state"] == "done" and report["attempts"] == 1
    assert report["audit_chain_intact"] is True and report["cost_eur"] == 0.0


def test_a_local_only_runtime_keeps_supervisor_turns_local() -> None:
    from uuid import uuid4

    from jarvis_core.agents.runtime import AgentTurn, GatewayRuntime, TurnScope
    from jarvis_core.contracts.classification import DataClassification, Domain
    from jarvis_core.router.budget import CircuitBreaker, InMemorySpendLedger
    from jarvis_core.router.catalog import LOCAL_MODELS, ModelClass
    from jarvis_core.router.gateway import InferenceGateway

    local = Answers("")
    gateway = InferenceGateway(
        local=local,
        breaker=CircuitBreaker(InMemorySpendLedger()),
        audit=lambda _e: None,
        actor="router",
    )
    GatewayRuntime(gateway, Domain.CONTENT, require_local=True).run(
        AgentTurn(role="supervisor", model_class=ModelClass.SUPERVISOR, system="s", prompt="p"),
        TurnScope(
            task_id=uuid4(), trace_id="t", domain=Domain.CONTENT, label=DataClassification.PUBLIC
        ),
    )
    assert local.calls[0].model == LOCAL_MODELS[ModelClass.SUPERVISOR].name
