"""Runs inside the jarvis-reel-media sandbox image. Never imported by the host.

reel-media fetch <canonical reel URL>   -> /out/media.mp4, /out/info.json   (with egress)
reel-media extract                      -> /out/audio.wav, /out/frame_NN.jpg,
                                           /out/metadata.json            (no network)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

MAX_FILESIZE = "200M"
MAX_DURATION_S = 600
MAX_FRAMES = 12
SCENE_THRESHOLD = 0.3
FFMPEG = "/usr/bin/ffmpeg"
YTDLP = "/usr/local/bin/yt-dlp"
# Fixed mount points inside the sandbox; overridable only to test the script outside one.
IN_DIR = Path(os.environ.get("REEL_MEDIA_IN", "/in"))
OUT_DIR = Path(os.environ.get("REEL_MEDIA_OUT", "/out"))


def fetch(url: str) -> int:
    out = OUT_DIR
    cmd = [
        YTDLP,
        "--no-playlist",
        "--no-progress",
        "--no-cache-dir",
        "--no-config",
        "--restrict-filenames",
        "--max-filesize",
        MAX_FILESIZE,
        "--match-filter",
        f"duration <= {MAX_DURATION_S}",
        "-f",
        "best[ext=mp4]/best",
        "--remux-video",
        "mp4",
        "--write-info-json",
        "-o",
        str(out / "media.%(ext)s"),
        "--",
        url,
    ]
    proc = subprocess.run(cmd, check=False)  # noqa: S603
    if proc.returncode != 0:
        return proc.returncode
    info = out / "media.info.json"
    if info.exists():
        info.rename(out / "info.json")
    return 0 if (out / "media.mp4").exists() else 3


def extract() -> int:
    src, out = IN_DIR, OUT_DIR
    media = src / "media.mp4"
    audio = subprocess.run(  # noqa: S603
        [
            FFMPEG,
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(media),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(out / "audio.wav"),
        ],
        check=False,
    )
    if audio.returncode != 0:
        return audio.returncode
    frames = subprocess.run(  # noqa: S603
        [
            FFMPEG,
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(media),
            "-vf",
            f"select='eq(n,0)+gt(scene,{SCENE_THRESHOLD})',scale='min(1024,iw)':-2",
            "-fps_mode",
            "vfr",
            "-frames:v",
            str(MAX_FRAMES),
            "-q:v",
            "4",
            str(out / "frame_%02d.jpg"),
        ],
        check=False,
    )
    if frames.returncode != 0:
        return frames.returncode

    info_file = src / "info.json"
    info = json.loads(info_file.read_text(encoding="utf-8")) if info_file.exists() else {}
    metadata = {
        "webpage_url": str(info.get("webpage_url", ""))[:500],
        "caption": str(info.get("description") or "")[:5000],
        "uploader": str(info.get("uploader") or "")[:200],
        "upload_date": info.get("upload_date") if _is_date(info.get("upload_date")) else None,
        "duration_s": float(info.get("duration") or 0.0),
    }
    (out / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    return 0


def _is_date(value: object) -> bool:
    return isinstance(value, str) and len(value) == 8 and value.isdigit()


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "fetch":
        return fetch(sys.argv[2])
    if len(sys.argv) == 2 and sys.argv[1] == "extract":
        return extract()
    print("usage: reel-media fetch <url> | reel-media extract", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
