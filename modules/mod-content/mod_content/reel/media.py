"""Deterministic checks on what the sandboxes wrote. Nothing here trusts the files.

Layout of one run's work directory (each step writes only its own subdirectory):

    fetch/      media.mp4, info.json            (yt-dlp, with egress)
    extract/    audio.wav, frame_NN.jpg, metadata.json   (ffmpeg, no network)
    transcribe/ transcript.json                 (Faster-Whisper, no network)
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from mod_content.reel.schemas import ReelMetadata, Transcript

MAX_MEDIA_BYTES = 200 * 1024 * 1024
MAX_AUDIO_BYTES = 120 * 1024 * 1024
MAX_FRAME_BYTES = 2 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_FRAMES = 12

_FRAME_NAME = re.compile(r"^frame_\d{2}\.jpg$")
_ALLOWED = {
    "fetch": re.compile(r"^(media\.mp4|info\.json)$"),
    "extract": re.compile(r"^(audio\.wav|metadata\.json|frame_\d{2}\.jpg)$"),
    "transcribe": re.compile(r"^transcript\.json$"),
}


class MediaRejected(Exception):
    """The sandbox output is malformed, oversized or contains unexpected files."""


@dataclass(frozen=True)
class MediaBundle:
    media_hash: str
    metadata: ReelMetadata
    frames: tuple[Path, ...]
    audio: Path


def check_step_dir(work: Path, step: str) -> list[Path]:
    """Every entry must be a regular file with an expected name. No symlinks, no subdirs."""
    directory = work / step
    if directory.is_symlink() or not directory.is_dir():
        raise MediaRejected(f"{step}/ is missing")
    files: list[Path] = []
    for entry in sorted(directory.iterdir()):
        if entry.is_symlink() or not entry.is_file():
            raise MediaRejected(f"{step}/ contains a non-regular entry")
        if not _ALLOWED[step].fullmatch(entry.name):
            raise MediaRejected(f"{step}/ contains an unexpected file")
        files.append(entry)
    return files


def load_fetch(work: Path) -> str:
    """Validate the download and return the SHA-256 of the media file."""
    files = {f.name: f for f in check_step_dir(work, "fetch")}
    media = files.get("media.mp4")
    if media is None:
        raise MediaRejected("fetch produced no media")
    _limit(media, MAX_MEDIA_BYTES)
    head = _head(media)
    if head[4:8] != b"ftyp":
        raise MediaRejected("media is not an MP4 container")
    return _sha256_file(media)


def load_extract(work: Path, media_hash: str) -> MediaBundle:
    files = {f.name: f for f in check_step_dir(work, "extract")}
    audio = files.get("audio.wav")
    meta_file = files.get("metadata.json")
    if audio is None or meta_file is None:
        raise MediaRejected("extract is missing audio or metadata")
    _limit(audio, MAX_AUDIO_BYTES)
    head = _head(audio)
    if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        raise MediaRejected("audio is not a WAV file")
    frames = tuple(sorted(f for name, f in files.items() if _FRAME_NAME.fullmatch(name)))
    if len(frames) > MAX_FRAMES:
        raise MediaRejected(f"more than {MAX_FRAMES} keyframes")
    for frame in frames:
        _limit(frame, MAX_FRAME_BYTES)
        if _head(frame)[:3] != b"\xff\xd8\xff":
            raise MediaRejected("keyframe is not a JPEG")
    try:
        metadata = ReelMetadata.model_validate(_read_json(meta_file))
    except ValidationError as exc:
        raise MediaRejected(f"metadata.json invalid: {exc.error_count()} errors") from exc
    return MediaBundle(media_hash=media_hash, metadata=metadata, frames=frames, audio=audio)


def load_transcript(work: Path) -> Transcript:
    files = {f.name: f for f in check_step_dir(work, "transcribe")}
    file = files.get("transcript.json")
    if file is None:
        raise MediaRejected("transcribe produced no transcript")
    try:
        return Transcript.model_validate(_read_json(file))
    except ValidationError as exc:
        raise MediaRejected(f"transcript.json invalid: {exc.error_count()} errors") from exc


def _read_json(file: Path) -> object:
    _limit(file, MAX_JSON_BYTES)
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MediaRejected(f"{file.name} is not valid JSON") from exc


def _head(file: Path) -> bytes:
    with file.open("rb") as fh:
        return fh.read(12)


def _limit(file: Path, max_bytes: int) -> None:
    if file.stat().st_size > max_bytes:
        raise MediaRejected(f"{file.name} exceeds {max_bytes} bytes")


def _sha256_file(file: Path) -> str:
    h = hashlib.sha256()
    with file.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
