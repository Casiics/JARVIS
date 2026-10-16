"""Fakes for the reel pipeline: a sandbox runner that writes plausible files, a model client."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from jarvis_core.router.client import ModelCall, ModelResult
from mod_content.reel.sandbox import SandboxResult, SandboxSpec

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64

TRANSCRIPT_TEXT = "Use ripgrep instead of grep. Install it with your package manager."


def good_draft(**overrides: object) -> str:
    draft: dict[str, object] = {
        "summary": "The video shows how to search large codebases quickly with ripgrep.",
        "extracted_tools": ["ripgrep"],
        "actionable_steps": ["Install ripgrep", "Replace grep -r with rg in scripts"],
        "target_module": "mod-self-evolution",
        "utility_score": 0.7,
    }
    draft.update(overrides)
    return json.dumps(draft)


class FakeRunner:
    def __init__(
        self,
        *,
        fail_step: str | None = None,
        transcript: str = TRANSCRIPT_TEXT,
        on_run: Callable[[str], None] | None = None,
    ) -> None:
        self.specs: list[SandboxSpec] = []
        self._fail = fail_step
        self._transcript = transcript
        self._on_run = on_run

    def run(self, spec: SandboxSpec) -> SandboxResult:
        self.specs.append(spec)
        if self._on_run:
            self._on_run(spec.step)
        if spec.step == self._fail:
            return SandboxResult(
                step=spec.step, exit_code=1, duration_ms=5, stderr_sha256=_sha(b"")
            )
        out = next(m.source for m in spec.mounts if m.target == "/out")
        if spec.step == "fetch":
            (out / "media.mp4").write_bytes(MP4)
            (out / "info.json").write_text("{}")
        elif spec.step == "extract":
            (out / "audio.wav").write_bytes(WAV)
            (out / "frame_01.jpg").write_bytes(JPEG)
            (out / "frame_02.jpg").write_bytes(JPEG)
            (out / "metadata.json").write_text(
                json.dumps(
                    {
                        "webpage_url": "https://www.instagram.com/reel/ABCDE12345/",
                        "caption": "Stop using grep!",
                        "duration_s": 31.5,
                    }
                )
            )
        elif spec.step == "transcribe":
            (out / "transcript.json").write_text(
                json.dumps(
                    {
                        "language": "en",
                        "model": "faster-whisper/large-v3",
                        "segments": [{"start_s": 0, "end_s": 5, "text": self._transcript}],
                    }
                )
            )
        return SandboxResult(step=spec.step, exit_code=0, duration_ms=10, stderr_sha256=_sha(b""))


class FakeModel:
    """Vision calls return OCR text; extraction calls return the scripted answers in order."""

    def __init__(self, answers: list[str], loaded: list[str] | None = None) -> None:
        self.answers = list(answers)
        self.calls: list[ModelCall] = []
        self.loaded = loaded if loaded is not None else ["ollama/deepseek-r1:8b"]
        self.unloaded: list[str] = []
        self.events: list[str] = []

    def complete(self, call: ModelCall) -> ModelResult:
        self.calls.append(call)
        self.events.append(f"complete:{call.model}")
        if any(m.images_b64 for m in call.messages):
            return ModelResult(text="$ rg TODO src/", prompt_tokens=1000, completion_tokens=10)
        text = self.answers.pop(0) if self.answers else "not json"
        return ModelResult(text=text, prompt_tokens=800, completion_tokens=120)

    def loaded_models(self) -> list[str]:
        return list(self.loaded)

    def unload(self, model: str) -> None:
        self.events.append(f"unload:{model}")
        self.unloaded.append(model)
        self.loaded = [m for m in self.loaded if m != model]


def write_dir(base: Path, step: str, files: dict[str, bytes]) -> None:
    (base / step).mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (base / step / name).write_bytes(data)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
