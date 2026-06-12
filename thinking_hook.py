#!/usr/bin/env python3
"""
CC CLI Stop hook — extract thinking and POST to gateway.
Tries hook_input.last_assistant_message first, falls back to transcript.
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


def _extract_thinking(content):
    parts = []
    if not isinstance(content, list):
        return parts
    for block in content:
        if isinstance(block, dict) and block.get("type") == "thinking":
            t = block.get("thinking", "")
            if t:
                parts.append(t)
    return parts


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

    _log(f"keys: {list(hook_input.keys())}")

    # Method 1: last_assistant_message from hook input
    last_msg = hook_input.get("last_assistant_message", {})
    if isinstance(last_msg, dict):
        content = last_msg.get("content", [])
        _log(f"last_msg content types: {[b.get('type','?') for b in content if isinstance(b, dict)]}")
        thinking_parts = _extract_thinking(content)
        if thinking_parts:
            _log(f"method=last_msg, {len(''.join(thinking_parts))} chars")
            _post(thinking_parts)
            return

    # Method 2: read transcript
    transcript_path = hook_input.get("transcript_path", "")
    if not transcript_path or not Path(transcript_path).exists():
        _log(f"no transcript: {transcript_path}")
        return

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
        content = entry.get("message", {}).get("content", [])
        _log(f"transcript content types: {[b.get('type','?') for b in content if isinstance(b, dict)]}")
        thinking_parts = _extract_thinking(content)
        if thinking_parts:
            _log(f"method=transcript, {len(''.join(thinking_parts))} chars")
            _post(thinking_parts)
            return
        break

    _log("no thinking found anywhere")


def _post(parts):
    thinking_text = "\n".join(parts)
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
