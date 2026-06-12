#!/usr/bin/env python3
"""
CC CLI Stop hook — extract thinking from transcript and POST to gateway.
"""

import json
import sys
import os
from pathlib import Path
from urllib.request import urlopen, Request
from datetime import datetime

GATEWAY_URL = os.getenv("THINKING_HOOK_URL", "http://127.0.0.1:3000/internal/thinking")
LOG = Path("/tmp/thinking_hook.log")


def _log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%H:%M:%S} {msg}\n")


def main():
    try:
        raw = sys.stdin.read()
    except Exception:
        _log("stdin read failed")
        return
    if not raw.strip():
        _log("stdin empty")
        return
    try:
        hook_input = json.loads(raw)
    except json.JSONDecodeError:
        _log("stdin not json")
        return

    transcript_path = hook_input.get("transcript_path", "")
    if not transcript_path:
        session_id = hook_input.get("session_id", "")
        if session_id:
            cwd = hook_input.get("cwd", "/opt/G-memory-mcp")
            home = Path.home()
            slug = cwd.replace("/", "-")
            candidates = list((home / ".claude" / "projects" / slug).glob("*.jsonl"))
            if candidates:
                transcript_path = str(max(candidates, key=lambda p: p.stat().st_mtime))

    if not transcript_path or not Path(transcript_path).exists():
        _log(f"no transcript: {transcript_path}")
        return

    thinking_parts = []
    with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message", {})
        content = msg.get("content", [])
        for block in content:
            if isinstance(block, dict) and block.get("type") == "thinking":
                t = block.get("thinking", "")
                if t:
                    thinking_parts.append(t)
        break

    if not thinking_parts:
        _log(f"no thinking found in {len(lines)} lines")
        return

    thinking_text = "\n".join(thinking_parts)
    _log(f"found {len(thinking_text)} chars thinking")

    try:
        payload = json.dumps({"thinking": thinking_text}, ensure_ascii=False).encode("utf-8")
        req = Request(GATEWAY_URL, data=payload,
                      headers={"Content-Type": "application/json"}, method="POST")
        resp = urlopen(req, timeout=5)
        _log(f"POST ok: {resp.status}")
    except Exception as e:
        _log(f"POST failed: {e}")


if __name__ == "__main__":
    main()
