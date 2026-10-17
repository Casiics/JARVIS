"""Pick the agent runtime for a domain's local work.

Hermes Agent is the default runtime wherever it is installed: it runs each turn as a one-shot
agent whose model calls still go through the domain's inference gateway (`agents.hermes`).
Without the `hermes` binary the domain falls back to plain gateway completions, so the same
loop runs everywhere. Both are pinned to local models: `require_local` fails closed instead of
reaching for a cloud model.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from typing import Literal

from jarvis_core.agents.hermes import HermesRuntime
from jarvis_core.agents.runtime import AgentRuntime, GatewayRuntime
from jarvis_core.contracts.classification import Domain
from jarvis_core.router.gateway import InferenceGateway

RuntimeChoice = Literal["auto", "hermes", "gateway"]


class RuntimeUnavailable(Exception):
    pass


def local_runtime(
    gateway: InferenceGateway,
    domain: Domain,
    *,
    choice: RuntimeChoice = "auto",
    hermes_command: Sequence[str] = ("hermes",),
    which: Callable[[str], str | None] = shutil.which,
) -> AgentRuntime:
    """Hermes if available (or asked for), otherwise the gateway; local models only."""
    has_hermes = bool(hermes_command) and which(hermes_command[0]) is not None
    if choice == "hermes" and not has_hermes:
        raise RuntimeUnavailable(f"{hermes_command[0]!r} is not installed")
    if choice == "hermes" or (choice == "auto" and has_hermes):
        return HermesRuntime(gateway, domain, command=tuple(hermes_command), require_local=True)
    return GatewayRuntime(gateway, domain, require_local=True)


def runtime_name(runtime: AgentRuntime) -> str:
    return "hermes" if isinstance(runtime, HermesRuntime) else "gateway"
