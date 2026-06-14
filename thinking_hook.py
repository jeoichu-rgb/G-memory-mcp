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
import time
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen, Request

GATEWAY_URL = os.getenv("THINKING_HOOK_URL", "http://127.0.0.1:3000/internal/thinking")
TMUX_SESSION = os.getenv("CC_TMUX_SESSION", "cc_cli")
LOG = Path("/tmp/thinking_hook.log")
LAST_THINKING_PATH = Path("/tmp/thinking_hook_last.txt")


def _log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%H:%M:%S} {msg}\n")


def _capture_pane():
    return subprocess.check_output(
        ["tmux", "capture-pane", "-t", TMUX_SESSION, "-p", "-S", "-500"],
        text=True, timeout=5
    )


def _extract_thinking(pane):
    marker = "∴ Thinking"
    parts = pane.split(marker)
    if len(parts) < 2:
        return None
    last_section = parts[-1]
    response_marker = "\n●"
    idx = last_section.find(response_marker)
    if idx == -1:
        return None
    raw = last_section[:idx]
    raw = re.sub(r'^…?\s*\n', '', raw)
    lines = raw.split('\n')
    return '\n'.join(line.lstrip() for line in lines).strip() or None


def main():
    # First capture — might be too early (thinking not rendered yet)
    try:
        pane = _capture_pane()
    except Exception as e:
        _log(f"tmux capture failed: {e}")
        return

    cleaned = _extract_thinking(pane)

    # Read previous thinking for dedup
    try:
        prev = LAST_THINKING_PATH.read_text(encoding="utf-8") if LAST_THINKING_PATH.exists() else ""
    except Exception:
        prev = ""

    # If we got nothing or same as last time, wait and retry once
    if not cleaned or cleaned == prev:
        time.sleep(1)
        try:
            pane = _capture_pane()
        except Exception as e:
            _log(f"tmux retry capture failed: {e}")
            return
        cleaned = _extract_thinking(pane)

    if not cleaned:
        _log("thinking was empty after retry")
        return

    if cleaned == prev:
        _log(f"dedup: same as last after retry ({len(cleaned)} chars), skipping")
        return

    _log(f"captured {len(cleaned)} chars")

    try:
        payload = json.dumps({"thinking": cleaned}, ensure_ascii=False).encode("utf-8")
        req = Request(GATEWAY_URL, data=payload,
                      headers={"Content-Type": "application/json"}, method="POST")
        resp = urlopen(req, timeout=5)
        _log(f"POST ok: {resp.status}")
        LAST_THINKING_PATH.write_text(cleaned, encoding="utf-8")
    except Exception as e:
        _log(f"POST failed: {e}")


if __name__ == "__main__":
    main()
