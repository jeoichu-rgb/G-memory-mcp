"""
Analyze CC CLI transcript JSONL — per-message usage breakdown.

Usage:
  python3 analyze_usage.py                          # latest transcript
  python3 analyze_usage.py <session-uuid>           # specific session
  python3 analyze_usage.py --last 10                # last 10 entries only
"""

import json
import sys
from pathlib import Path

TRANSCRIPT_DIR = Path("/home/erik/.claude/projects/-opt-G-memory-mcp")


def fmt(n):
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


def estimate_tokens(text):
    if not text:
        return 0
    return len(text) // 3


def analyze(path, last_n=None):
    entries = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if last_n:
        entries = entries[-last_n:]

    print(f"Transcript: {path.name}")
    print(f"Total entries: {len(entries)}")
    print("=" * 90)

    turn = 0
    for i, entry in enumerate(entries):
        etype = entry.get("type", "?")

        if etype == "user":
            msg = entry.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, list):
                text = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
            else:
                text = str(content)
            est = estimate_tokens(text)
            turn += 1
            print(f"\n{'─'*90}")
            print(f"  Turn {turn} | USER | ~{fmt(est)} tokens est")
            preview = text.replace("\n", " ")[:80]
            print(f"  {preview}")

        elif etype == "assistant":
            msg = entry.get("message", {})
            usage = msg.get("usage", {})
            inp = usage.get("input_tokens", 0)
            out = usage.get("output_tokens", 0)
            cr = usage.get("cache_read_input_tokens", 0)
            cc = usage.get("cache_creation_input_tokens", 0)
            total_ctx = inp + cr + cc
            stop = msg.get("stop_reason", "")
            model = msg.get("model", "?")

            blocks = msg.get("content", [])
            thinking_len = 0
            text_len = 0
            tool_blocks = []
            for blk in blocks:
                if not isinstance(blk, dict):
                    continue
                bt = blk.get("type", "")
                if bt == "thinking":
                    thinking_len += len(blk.get("thinking", "") or "")
                elif bt == "text":
                    text_len += len(blk.get("text", "") or "")
                elif bt == "tool_use":
                    name = blk.get("name", "")
                    inp_size = len(json.dumps(blk.get("input", {}), ensure_ascii=False))
                    tool_blocks.append((name, inp_size))
                elif bt == "tool_result":
                    content = blk.get("content", "")
                    if isinstance(content, list):
                        result_text = " ".join(
                            b.get("text", "") for b in content if isinstance(b, dict)
                        )
                    else:
                        result_text = str(content)
                    tool_blocks.append(("  └ result", len(result_text)))

            if not usage:
                continue

            print(f"\n  ASSISTANT | {model} | stop={stop}")
            print(f"  ┌ Usage:  ↑{fmt(inp)}  cache_read={fmt(cr)}  +cache={fmt(cc)}  ↓{fmt(out)}")
            print(f"  │ Context window: {fmt(total_ctx)} tokens ({total_ctx*100//200000}% of 200k)")
            if thinking_len:
                print(f"  │ Thinking: {fmt(estimate_tokens(chr(0)*thinking_len))} tokens est ({thinking_len} chars)")
            if text_len:
                print(f"  │ Reply text: {fmt(estimate_tokens(chr(0)*text_len))} tokens est ({text_len} chars)")
            for name, size in tool_blocks:
                print(f"  │ Tool: {name} ({fmt(estimate_tokens(chr(0)*size))} tokens est)")
            print(f"  └ cache delta: +cache grew by {fmt(cc)} (new content entering cache)")

        elif etype == "summary":
            print(f"\n  *** COMPACTION ***")
            summary = entry.get("summary", "")
            if summary:
                preview = summary.replace("\n", " ")[:120]
                print(f"  {preview}...")

    print(f"\n{'='*90}")


def main():
    target = None
    last_n = None

    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--last" and i + 1 < len(args):
            last_n = int(args[i + 1])
        elif not a.startswith("-"):
            target = a

    if target:
        path = TRANSCRIPT_DIR / f"{target}.jsonl"
        if not path.exists():
            candidates = list(TRANSCRIPT_DIR.glob(f"{target}*.jsonl"))
            if candidates:
                path = candidates[0]
            else:
                print(f"Not found: {target}")
                sys.exit(1)
    else:
        candidates = list(TRANSCRIPT_DIR.glob("*.jsonl"))
        if not candidates:
            print("No transcripts found")
            sys.exit(1)
        path = max(candidates, key=lambda p: p.stat().st_mtime)

    analyze(path, last_n)


if __name__ == "__main__":
    main()
