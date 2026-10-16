"""Why a reel could not be fetched: fixed exit codes from the sandbox, readable classes outside.

yt-dlp's messages are untrusted text and never leave the sandbox. Only the exit code does, so
the class reported to core and shown in the deck is chosen by deterministic code.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from mod_content.reel.pipeline import FETCH_EXIT_CLASSES, sandbox_error_class
from mod_content.reel.sandbox import EngineConfig, SandboxResult, preflight

HASH = "a" * 64
REEL_MEDIA = Path(__file__).parents[2] / "modules/mod-content/sandbox/reel_media.py"


def _reel_media() -> ModuleType:
    spec = importlib.util.spec_from_file_location("reel_media_under_test", REEL_MEDIA)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reel_media = _reel_media()


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (
            "ERROR: [Instagram] ABC: Requested content is not available, rate-limit reached or "
            "login required. Use --cookies, --cookies-from-browser, --username and --password",
            "fetch_login_required",
        ),
        ("ERROR: [Instagram] ABC: HTTP Error 429: Too Many Requests", "fetch_login_required"),
        ("ERROR: [Instagram] ABC: Instagram sent an empty media response", "fetch_not_available"),
        ("ERROR: [Instagram] ABC: HTTP Error 404: Not Found", "fetch_not_available"),
        (
            "ERROR: Unable to download webpage: ('Unable to connect to proxy', "
            "OSError('Tunnel connection failed: 403 Forbidden'))",
            "fetch_proxy_unreachable",
        ),
        (
            "ERROR: Unable to download webpage: <urlopen error [Errno -3] "
            "Temporary failure in name resolution>",
            "fetch_network_error",
        ),
        ("ERROR: Read timed out.", "fetch_network_error"),
        (
            "[info] File is larger than max-filesize (99 bytes > 1 bytes). Aborting.",
            "fetch_too_large",
        ),
    ],
)
def test_yt_dlp_errors_map_to_named_classes(stderr: str, expected: str) -> None:
    code = reel_media.classify_fetch_error(stderr)
    result = SandboxResult(step="fetch", exit_code=code, duration_ms=1, stderr_sha256=HASH)
    assert sandbox_error_class("fetch", result) == expected


def test_unknown_errors_stay_generic() -> None:
    code = reel_media.classify_fetch_error("ERROR: something nobody has seen before")
    assert code == reel_media.EXIT_UNKNOWN
    result = SandboxResult(step="fetch", exit_code=code, duration_ms=1, stderr_sha256=HASH)
    assert sandbox_error_class("fetch", result) == "fetch_failed"


def test_sandbox_and_worker_agree_on_the_exit_codes() -> None:
    assert FETCH_EXIT_CLASSES == {
        reel_media.EXIT_LOGIN_REQUIRED: "fetch_login_required",
        reel_media.EXIT_NOT_AVAILABLE: "fetch_not_available",
        reel_media.EXIT_PROXY: "fetch_proxy_unreachable",
        reel_media.EXIT_NETWORK: "fetch_network_error",
        reel_media.EXIT_TOO_LARGE: "fetch_too_large",
        reel_media.EXIT_NO_VIDEO: "fetch_no_video",
    }
    assert reel_media.EXIT_UNKNOWN not in FETCH_EXIT_CLASSES


@pytest.mark.parametrize(
    ("step", "exit_code", "timed_out", "expected"),
    [
        ("fetch", 125, False, "fetch_sandbox_start_failed"),
        ("extract", 126, False, "extract_sandbox_start_failed"),
        ("transcribe", -1, True, "transcribe_timeout"),
        # Fetch codes mean nothing for other steps.
        ("extract", 20, False, "extract_failed"),
    ],
)
def test_engine_and_timeout_errors(
    step: str, exit_code: int, timed_out: bool, expected: str
) -> None:
    result = SandboxResult(
        step=step, exit_code=exit_code, duration_ms=1, timed_out=timed_out, stderr_sha256=HASH
    )
    assert sandbox_error_class(step, result) == expected


# --- preflight --------------------------------------------------------------------------------


class FakeEngine:
    """Answers `docker <args>` like a host where only the listed things exist."""

    def __init__(
        self,
        *,
        running: bool = True,
        images: tuple[str, ...] = (),
        network: bool = True,
        proxy_up: bool = True,
    ) -> None:
        self.running, self.images, self.network, self.proxy_up = running, images, network, proxy_up

    def __call__(self, argv: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        args = argv[1:]
        ok = {
            "info": self.running,
            "image": len(args) > 2 and args[2] in self.images,
            "network": self.network,
            "ps": True,
        }[args[0]]
        out = b"abc123\n" if args[0] == "ps" and self.proxy_up else b""
        return subprocess.CompletedProcess(argv, 0 if ok else 1, out, b"")


IMAGES = ("localhost/jarvis-reel-media:0.1.0", "localhost/jarvis-reel-whisper:0.1.0")
DOCKER = EngineConfig(engine="docker")


def test_ready_host_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", FakeEngine(images=IMAGES))
    assert preflight(DOCKER, IMAGES) == []


def test_engine_down_is_reported_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", FakeEngine(running=False))
    [problem] = preflight(DOCKER, IMAGES)
    assert "docker info" in problem


def test_missing_images_and_proxy_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", FakeEngine(images=IMAGES[:1], proxy_up=False))
    problems = preflight(DOCKER, IMAGES)
    assert len(problems) == 2
    assert IMAGES[1] in problems[0] and "docker build" in problems[0]
    assert "egress-reel" in problems[1] and "not running" in problems[1]


def test_missing_network_points_at_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", FakeEngine(images=IMAGES, network=False))
    [problem] = preflight(DOCKER, IMAGES)
    assert "jarvis-sandbox-egress" in problem and "up -d egress-reel" in problem


def test_failed_step_prints_its_error_only_when_asked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from mod_content.reel.sandbox import SandboxMount, SandboxSpec, SubprocessSandboxRunner

    def fail(argv: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 20, b"", b"ERROR: login required")

    monkeypatch.setattr(subprocess, "run", fail)
    spec = SandboxSpec(
        step="extract",
        image=IMAGES[0],
        command=("reel-media", "extract"),
        mounts=(SandboxMount(source=tmp_path, target="/out", read_only=False),),
    )
    SubprocessSandboxRunner(DOCKER).run(spec)
    assert capsys.readouterr().err == ""
    result = SubprocessSandboxRunner(EngineConfig(engine="docker", show_errors=True)).run(spec)
    assert "login required" in capsys.readouterr().err
    assert result.exit_code == 20 and len(result.stderr_sha256) == 64


def test_the_deck_explains_every_fetch_class() -> None:
    format_ts = (Path(__file__).parents[2] / "apps/deck/src/format.ts").read_text()
    for error_class in (*FETCH_EXIT_CLASSES.values(), "fetch_failed", "fetch_sandbox_start_failed"):
        assert f"  {error_class}:" in format_ts, error_class
