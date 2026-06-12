#!/usr/bin/env python3
"""
CC CLI Stop hook — capture thinking summary from tmux pane and POST to gateway.
showThinkingSummaries must be true in settings for this to work.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen, Request

GATEWAY_URL = os.getenv("THINKING_HOOK_URL", "http://127.0.0.1:3000/internal/thinking")
TMUX_SESSION = os.getenv("CC_TMUX_SESSION", "cc_cli")
LOG = Path("/tmp/thinking_hook.log")


def _log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%H:%M:%S} {msg}\n")


def main():
    # Capture tmux pane content (last 500 lines)
    try:
        pane = subprocess.check_output(
            ["tmux", "capture-pane", "-t", TMUX_SESSION, "-p", "-S", "-500"],
            text=True, timeout=5
        )
    except Exception as e:
        _log(f"tmux capture failed: {e}")
        return

    # Find the LAST thinking block: between "∴ Thinking…" and "●"
    # Split into sections by the thinking marker
    marker = "∴ Thinking"
    parts = pane.split(marker)
    if len(parts) < 2:
        _log("no thinking marker found")
        return

    # Take the last thinking section
    last_section = parts[-1]

    # Extract text between the marker and the response marker "●"
    response_marker = "\n●"
    idx = last_section.find(response_marker)
    if idx == -1:
        _log("no response marker ● found after thinking")
        return

    raw_thinking = last_section[:idx]
    # Clean up: remove leading "…\n", strip indentation
    raw_thinking = re.sub(r'^…?\s*\n', '', raw_thinking)
    lines = raw_thinking.split('\n')
    cleaned = '\n'.join(line.lstrip() for line in lines).strip()

    if not cleaned:
        _log("thinking was empty after cleanup")
        return

    _log(f"captured {len(cleaned)} chars")

    try:
        payload = json.dumps({"thinking": cleaned}, ensure_ascii=False).encode("utf-8")
        req = Request(GATEWAY_URL, data=payload,
                      headers={"Content-Type": "application/json"}, method="POST")
        resp = urlopen(req, timeout=5)
        _log(f"POST ok: {resp.status}")
    except Exception as e:
        _log(f"POST failed: {e}")


if __name__ == "__main__":
    main()
