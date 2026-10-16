"""Sandbox runner for untrusted work: yt-dlp, ffmpeg on downloaded media, Faster-Whisper.

Each step runs in a fresh, rootless container: read-only root filesystem, all capabilities
dropped, no new privileges, no secrets, one writable output directory, resource limits and
no network unless the step is the download. Egress for the download goes through the
per-run egress proxy, which enforces the host allowlist (Phase 4); until that proxy exists
the allowlist is passed to it but not enforced by the network (see docs/runbooks).
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Hosts the reel download may reach (Instagram pages and its CDNs).
REEL_EGRESS_ALLOWLIST: tuple[str, ...] = (
    "instagram.com",
    "www.instagram.com",
    "i.instagram.com",
    "cdninstagram.com",
    "fbcdn.net",
)

_FORBIDDEN_MOUNT_SOURCES: tuple[PurePosixPath, ...] = (
    PurePosixPath("/run/jarvis"),
    PurePosixPath("/etc"),
    PurePosixPath("/var/run"),
    PurePosixPath("/run/docker.sock"),
    PurePosixPath("/root"),
    PurePosixPath("/proc"),
    PurePosixPath("/sys"),
    PurePosixPath("/dev"),
)
_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE)", re.I)
_PINNED_IMAGE = re.compile(r"^[a-z0-9./_-]+(:[A-Za-z0-9._-]+)?(@sha256:[0-9a-f]{64})?$")


class SandboxPolicyError(ValueError):
    """The requested sandbox would weaken isolation."""


class SandboxMount(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Path
    target: str = Field(pattern=r"^/(in|out|models)(/[A-Za-z0-9._-]+)*$")
    read_only: bool = True

    @field_validator("source")
    @classmethod
    def _safe_source(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise SandboxPolicyError("mount sources must be absolute")
        posix = PurePosixPath(value.as_posix())
        if ".." in posix.parts:
            raise SandboxPolicyError("mount sources must not contain '..'")
        for forbidden in _FORBIDDEN_MOUNT_SOURCES:
            if posix == forbidden or forbidden in posix.parents:
                raise SandboxPolicyError(f"{posix} may not be mounted into a sandbox")
        if posix.name in {"docker.sock", "podman.sock"}:
            raise SandboxPolicyError("container sockets may not be mounted")
        return value


class SandboxSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    step: Literal["fetch", "extract", "transcribe"]
    image: str = Field(max_length=300)
    command: tuple[str, ...] = Field(min_length=1)
    mounts: tuple[SandboxMount, ...] = ()
    env: dict[str, str] = Field(default_factory=dict)
    egress_allowlist: tuple[str, ...] = ()
    gpu: bool = False
    memory_mb: int = Field(default=2048, ge=64, le=16384)
    cpus: float = Field(default=2.0, gt=0, le=16)
    pids: int = Field(default=256, ge=16, le=4096)
    timeout_s: int = Field(default=300, ge=1, le=3600)

    @field_validator("image")
    @classmethod
    def _pinned(cls, value: str) -> str:
        if not _PINNED_IMAGE.fullmatch(value) or value.endswith(":latest") or ":" not in value:
            raise SandboxPolicyError("sandbox images must be pinned to a tag or digest")
        return value

    @field_validator("env")
    @classmethod
    def _no_secrets(cls, value: dict[str, str]) -> dict[str, str]:
        for key in value:
            if _SECRET_ENV.search(key):
                raise SandboxPolicyError(f"environment variable {key} looks like a secret")
        return value

    @model_validator(mode="after")
    def _isolation(self) -> SandboxSpec:
        writable = [m for m in self.mounts if not m.read_only]
        if len(writable) > 1 or any(not m.target.startswith("/out") for m in writable):
            raise SandboxPolicyError("only one writable mount, at /out, is allowed")
        if self.egress_allowlist and self.step != "fetch":
            raise SandboxPolicyError("only the fetch step may have network egress")
        if self.gpu and self.step != "transcribe":
            raise SandboxPolicyError("only the transcribe step may use the GPU")
        return self


@dataclass(frozen=True)
class EngineConfig:
    """How sandboxes are started on this host."""

    engine: Literal["podman", "docker"] = "podman"
    runtime: str | None = None  # e.g. "runsc" for gVisor
    egress_network: str = "jarvis-sandbox-egress"
    egress_proxy: str | None = "http://egress-proxy:3128"
    user: str = "65534:65534"


def build_command(spec: SandboxSpec, name: str, config: EngineConfig) -> list[str]:
    cmd = [
        config.engine,
        "run",
        "--rm",
        "--name",
        name,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--user={config.user}",
        f"--memory={spec.memory_mb}m",
        f"--memory-swap={spec.memory_mb}m",
        f"--cpus={spec.cpus}",
        f"--pids-limit={spec.pids}",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=256m",
        "--ipc=none" if config.engine == "podman" else "--ipc=private",
        "--pull=never",
    ]
    if config.runtime:
        cmd.append(f"--runtime={config.runtime}")
    if spec.egress_allowlist:
        cmd.append(f"--network={config.egress_network}")
        env = dict(spec.env)
        if config.egress_proxy:
            env["HTTPS_PROXY"] = env["HTTP_PROXY"] = config.egress_proxy
        env["JARVIS_EGRESS_ALLOWLIST"] = ",".join(spec.egress_allowlist)
    else:
        cmd.append("--network=none")
        env = dict(spec.env)
    if spec.gpu:
        cmd.append("--device=nvidia.com/gpu=all" if config.engine == "podman" else "--gpus=all")
    for mount in spec.mounts:
        mode = "ro" if mount.read_only else "rw"
        cmd.append(f"--volume={mount.source}:{mount.target}:{mode},Z")
    for key in sorted(env):
        cmd.append(f"--env={key}={env[key]}")
    cmd.append(spec.image)
    cmd.extend(spec.command)
    return cmd


class SandboxResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    step: str
    exit_code: int
    duration_ms: int = Field(ge=0)
    timed_out: bool = False
    stderr_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class SandboxRunner(Protocol):
    def run(self, spec: SandboxSpec) -> SandboxResult: ...


class SubprocessSandboxRunner:
    """Starts one container per step. Output of untrusted tools is hashed, never logged."""

    def __init__(self, config: EngineConfig | None = None) -> None:
        self._config = config or EngineConfig()

    def run(self, spec: SandboxSpec) -> SandboxResult:
        name = f"jarvis-sbx-{spec.step}-{uuid4().hex[:12]}"
        cmd = build_command(spec, name, self._config)
        started = time.monotonic()
        try:
            proc = subprocess.run(  # noqa: S603 - argv list, no shell
                cmd, capture_output=True, timeout=spec.timeout_s, check=False
            )
            exit_code, stderr, timed_out = proc.returncode, proc.stderr, False
        except subprocess.TimeoutExpired as exc:
            subprocess.run(  # noqa: S603
                [self._config.engine, "kill", name], capture_output=True, check=False
            )
            exit_code, stderr, timed_out = -1, exc.stderr or b"", True
        return SandboxResult(
            step=spec.step,
            exit_code=exit_code,
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=timed_out,
            stderr_sha256=hashlib.sha256(stderr).hexdigest(),
        )
