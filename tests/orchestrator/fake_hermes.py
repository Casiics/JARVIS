"""Stands in for `hermes -z <prompt>`: reads its config, calls the endpoint, prints the answer.

Like Hermes Agent, it offers tools to the model and may ask for streaming; the scoped endpoint
must drop the tools. Behaviour switches come from the prompt, since the environment is scrubbed.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    assert args[0] == "-z", args
    prompt = args[1]
    config = (Path(os.environ["HOME"]) / ".hermes/config.yaml").read_text()
    values = {
        k.strip(): v.strip()
        for k, _, v in (line.partition(":") for line in config.splitlines())
        if v.strip()
    }
    base_url = config.split("base_url: ", 1)[1].splitlines()[0]
    key = values["api_key"]
    if "LEAK_CHECK" in prompt:
        leaked = sorted(k for k in os.environ if k not in {"HOME", "PATH", "LANG", "NO_COLOR"})
        print(json.dumps({"env": leaked}))
        if "WITHOUT_CALL" in prompt:
            return 0
    if "WRONG_KEY" in prompt:
        key = "guess"
    body = {
        "model": "jarvis",
        "messages": [
            {"role": "system", "content": "hermes system prompt"},
            {"role": "user", "content": [{"type": "text", "text": prompt}]},
        ],
        "tools": [{"type": "function", "function": {"name": "terminal", "parameters": {}}}],
        "stream": "STREAM" in prompt,
    }
    req = urllib.request.Request(  # noqa: S310 - loopback test endpoint
        base_url + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        print(f"http {exc.code}", file=sys.stderr)
        return 1
    if body["stream"]:
        text = ""
        for line in raw.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                delta = json.loads(line[6:])["choices"][0]["delta"]
                text += delta.get("content", "")
    else:
        text = json.loads(raw)["choices"][0]["message"]["content"]
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
