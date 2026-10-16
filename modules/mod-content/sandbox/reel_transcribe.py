"""Runs inside the jarvis-reel-whisper sandbox image (GPU, no network). Never imported by the host.

reel-transcribe --model large-v3 --compute-type int8_float16
reads /in/audio.wav and /models/<model>, writes /out/transcript.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from faster_whisper import WhisperModel  # type: ignore[import-not-found]

MAX_SEGMENTS = 2000


def main() -> int:
    parser = argparse.ArgumentParser(prog="reel-transcribe")
    parser.add_argument("--model", required=True)
    parser.add_argument("--compute-type", default="int8_float16")
    args = parser.parse_args()

    model_dir = Path("/models") / args.model
    model = WhisperModel(str(model_dir), device="cuda", compute_type=args.compute_type)
    segments, info = model.transcribe("/in/audio.wav", vad_filter=True, beam_size=5)
    items = []
    for segment in segments:
        items.append(
            {
                "start_s": max(segment.start, 0.0),
                "end_s": max(segment.end, 0.0),
                "text": segment.text[:2000],
            }
        )
        if len(items) >= MAX_SEGMENTS:
            break
    transcript = {
        "language": (info.language or "und")[:10],
        "segments": items,
        "model": f"faster-whisper/{args.model}"[:100],
    }
    Path("/out/transcript.json").write_text(json.dumps(transcript), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
