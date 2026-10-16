"""Hermes Agent behind the runtime seam: its only model is the task-scoped gateway endpoint."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from jarvis_core.agents.hermes import HermesRuntime, hermes_config
from jarvis_core.agents.runtime import AgentRuntimeError, AgentTurn, TurnScope
from jarvis_core.contracts.audit import AuditAction, AuditDecision, AuditLogEntry
from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.router.budget import CircuitBreaker, InMemorySpendLedger
from jarvis_core.router.catalog import ModelClass
from jarvis_core.router.gateway import InferenceGateway
from tests.orchestrator.fakes import ExplodingCloud, RecordingModel

FAKE = (sys.executable, str(Path(__file__).with_name("fake_hermes.py")))


class Rig:
    def __init__(self, domain: Domain = Domain.CONTENT) -> None:
        self.model = RecordingModel(answer='{"title": "ok"}')
        self.audit: list[AuditLogEntry] = []
        self.gateway = InferenceGateway(
            local=self.model,
            breaker=CircuitBreaker(InMemorySpendLedger()),
            audit=self.audit.append,
            actor="spiffe://jarvis.home/core/model-router",
            cloud=ExplodingCloud(),
            cloud_models={c: "cloud/x" for c in ModelClass},
        )
        self.domain = domain
        self.runtime = HermesRuntime(self.gateway, domain, command=FAKE, timeout_s=30)

    def run(self, prompt: str, label: DataClassification = DataClassification.PUBLIC) -> str:
        reply = self.runtime.run(
            AgentTurn(
                role="worker",
                model_class=ModelClass.REASONING,
                system="You are a worker.",
                prompt=prompt,
            ),
            TurnScope(task_id=uuid4(), trace_id="t" * 32, domain=self.domain, label=label),
        )
        return reply.text


def test_hermes_reaches_the_model_only_through_the_gateway_without_tools() -> None:
    rig = Rig()
    assert rig.run("Summarise this.") == '{"title": "ok"}'

    [call] = rig.model.calls
    assert call.model.startswith("ollama/")
    # Hermes offered a terminal tool; the model never saw it, only the text messages.
    assert all("terminal" not in m.content for m in call.messages)
    assert [m.role for m in call.messages] == ["system", "user"]
    [entry] = [e for e in rig.audit if e.action is AuditAction.MODEL_CALL]
    assert entry.decision is AuditDecision.ALLOW and entry.content_hash


def test_streaming_requests_are_answered_too() -> None:
    assert Rig().run("STREAM please") == '{"title": "ok"}'


def test_hermes_gets_a_scrubbed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-leak")
    out = Rig().run("LEAK_CHECK")
    assert json.loads(out.splitlines()[0]) == {"env": []}


def test_a_wrong_endpoint_key_is_refused() -> None:
    rig = Rig()
    with pytest.raises(AgentRuntimeError) as err:
        rig.run("WRONG_KEY")
    assert err.value.error_class == "hermes_failed"
    assert rig.model.calls == []


def test_a_run_without_any_model_call_is_not_an_answer() -> None:
    with pytest.raises(AgentRuntimeError):
        Rig().run("LEAK_CHECK WITHOUT_CALL")


def test_private_turns_never_leave_the_machine() -> None:
    rig = Rig(Domain.PRIVATE)
    rig.run("Plan my day.", DataClassification.PRIVATE)
    assert all(c.model.startswith("ollama/") for c in rig.model.calls)


def test_policy_denials_surface_as_routing_denied() -> None:
    rig = Rig()
    with pytest.raises(AgentRuntimeError) as err:
        rig.run("anything", DataClassification.SECRET)
    assert err.value.error_class == "routing_denied"
    assert rig.model.calls == []


def test_a_missing_binary_is_reported() -> None:
    rig = Rig()
    rig.runtime.command = ("/nonexistent/hermes",)
    with pytest.raises(AgentRuntimeError) as err:
        rig.run("x")
    assert err.value.error_class == "hermes_missing"


def test_a_runtime_for_one_domain_refuses_another() -> None:
    rig = Rig(Domain.CONTENT)
    rig.domain = Domain.TRADING
    with pytest.raises(AgentRuntimeError):
        rig.run("x")


def test_the_config_has_no_toolsets_and_only_the_scoped_endpoint() -> None:
    text = hermes_config("http://127.0.0.1:1234/v1", "k")
    assert "provider: custom" in text and "base_url: http://127.0.0.1:1234/v1" in text
    assert "cli: []" in text


def test_hermes_runs_on_the_long_context_agent_model() -> None:
    rig = Rig()
    rig.run("Summarise this.")
    [call] = rig.model.calls
    assert call.model == "ollama/hermes3:8b"
    assert call.context_tokens == 65536  # Hermes asks for at least 64k


def test_every_local_model_is_loaded_with_its_own_fixed_window() -> None:
    from jarvis_core.router.catalog import LOCAL_MODELS, LONG_CONTEXT_CODING, local_context

    assert local_context(LOCAL_MODELS[ModelClass.REASONING].name) == 8192
    assert local_context(LONG_CONTEXT_CODING.name) == 32768
    assert local_context("ollama/unknown") == 8192
