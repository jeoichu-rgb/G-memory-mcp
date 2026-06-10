#!/usr/bin/env python3
"""
CC CLI Stop hook — extract thinking from transcript before redaction.
Triggered after every assistant reply. Reads transcript JSONL,
extracts thinking blocks from the latest assistant message,
and POSTs them to the gateway.

Configured in .claude/settings.json:
  "hooks": { "Stop": [{ "type": "command", "command": "python3 /opt/G-memory-mcp/thinking_hook.py" }] }
"""

import json
import sys
import os
from pathlib import Path
from urllib.request import urlopen, Request

GATEWAY_URL = os.getenv("THINKING_HOOK_URL", "http://127.0.0.1:3000/internal/thinking")

def main():
    try:
        raw = sys.stdin.read()
    except Exception:
        return
    if not raw.strip():
        return
    try:
        hook_input = json.loads(raw)
    except json.JSONDecodeError:
        return

    # CC CLI passes session info including transcript path
    transcript_path = hook_input.get("transcript_path", "")
    # Fallback: try session_id to find transcript
    if not transcript_path:
        session_id = hook_input.get("session_id", "")
        if session_id:
            # Try common transcript locations
            cwd = hook_input.get("cwd", "/opt/G-memory-mcp")
            home = Path.home()
            slug = cwd.replace("/", "-")
            candidates = list((home / ".claude" / "projects" / slug).glob("*.jsonl"))
            if candidates:
                transcript_path = str(max(candidates, key=lambda p: p.stat().st_mtime))

    if not transcript_path or not Path(transcript_path).exists():
        return

    # Read last assistant entry from transcript
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
        break  # only need the last assistant message

    if not thinking_parts:
        return

    thinking_text = "\n".join(thinking_parts)

    # POST to gateway
    try:
        payload = json.dumps({"thinking": thinking_text}, ensure_ascii=False).encode("utf-8")
        req = Request(GATEWAY_URL, data=payload,
                      headers={"Content-Type": "application/json"}, method="POST")
        urlopen(req, timeout=5)
    except Exception:
        pass

if __name__ == "__main__":
    main()
