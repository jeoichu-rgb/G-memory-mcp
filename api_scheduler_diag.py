"""Structured diagnostics and terminal inspector for API cache scheduling.

Runtime writes one JSON object per line to logs/api_scheduler.jsonl.  The same
file doubles as an operator journal; this module can be run directly without
starting the gateway.
"""

from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib.request import urlopen


UTC8 = timezone(timedelta(hours=8))


def _base_dir() -> Path:
    return Path(os.getenv("CC_CWD", Path(__file__).resolve().parent))


def journal_path() -> Path:
    configured = os.getenv("API_SCHEDULER_JOURNAL", "").strip()
    return Path(configured) if configured else _base_dir() / "logs" / "api_scheduler.jsonl"


def state_path() -> Path:
    return _base_dir() / "pebbling_state.json"


def write_event(code: str, *, level: str = "info", **fields: Any) -> None:
    """Append one secret-free diagnostic marker; never break the gateway."""
    try:
        path = journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "time": datetime.now(UTC8).isoformat(timespec="seconds"),
            "ts": time.time(),
            "level": level,
            "code": code,
            **fields,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        # Diagnostics must never turn a successful model request into a failure.
        pass


def _read_state() -> dict:
    path = state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {exc}", "_path": str(path)}


def _recent_entries(limit: int) -> list[dict]:
    path = journal_path()
    if not path.exists():
        return []
    rows: deque[dict] = deque(maxlen=max(limit, 1))
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return list(rows)


def _is_problem(entry: dict) -> bool:
    if entry.get("level") in {"warning", "error", "critical"}:
        return True
    code = str(entry.get("code", ""))
    return any(word in code for word in ("ERROR", "MISS", "BREAKER", "404", "FAIL"))


def _print_entry(entry: dict) -> None:
    stamp = entry.get("time", "?")
    level = str(entry.get("level", "info")).upper()
    code = entry.get("code", "UNKNOWN")
    details = {k: v for k, v in entry.items()
               if k not in {"time", "ts", "level", "code"}}
    print(f"{stamp} {level:<7} {code} {json.dumps(details, ensure_ascii=False)}")


def command_status() -> int:
    raw = _read_state()
    state = raw.get("api_scheduler", {}) if isinstance(raw, dict) else {}
    if not isinstance(state, dict):
        state = {}
    last = float(state.get("last_api_request_at", 0) or 0)
    interval = int(os.getenv("API_SCHEDULER_INTERVAL", "3000"))
    summary = {
        "state_file": str(state_path()),
        "journal": str(journal_path()),
        "active": bool(state.get("active")),
        "gateway_session_id": state.get("gateway_session_id"),
        "logical_session_id": state.get("logical_session_id"),
        "sticky_session_id": state.get("sticky_session_id"),
        "api_key_id": state.get("api_key_id"),
        "last_api_request_at": (
            datetime.fromtimestamp(last, UTC8).isoformat(timespec="seconds")
            if last else None
        ),
        "next_due_at": (
            datetime.fromtimestamp(last + interval, UTC8).isoformat(timespec="seconds")
            if last else None
        ),
        "last_job": state.get("last_job"),
        "last_job_at": state.get("last_job_at"),
        "keepalive_misses": state.get("keepalive_misses", 0),
        "keepalive_breaker": bool(state.get("keepalive_breaker")),
        "last_keepalive_hit": state.get("last_keepalive_hit"),
        "last_keepalive_cached_tokens": state.get("last_keepalive_cached_tokens", 0),
        "last_keepalive_usage": state.get("last_keepalive_usage", {}),
        "last_keepalive_error": state.get("last_keepalive_error"),
    }
    if raw.get("_error"):
        summary["state_error"] = raw["_error"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def command_tail(limit: int, problems_only: bool = False) -> int:
    entries = _recent_entries(max(limit * 5 if problems_only else limit, limit))
    if problems_only:
        entries = [entry for entry in entries if _is_problem(entry)][-limit:]
    else:
        entries = entries[-limit:]
    for entry in entries:
        _print_entry(entry)
    return 0


def command_live() -> int:
    url = os.getenv(
        "API_SCHEDULER_STATUS_URL",
        "http://127.0.0.1:8081/api/pebbling/status",
    )
    try:
        with urlopen(url, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(json.dumps({
            "url": url,
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(data.get("api_scheduler", data), ensure_ascii=False, indent=2))
    return 0


def command_follow(problems_only: bool = False) -> int:
    path = journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    print(f"Following {path} (Ctrl-C to stop)")
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(0, os.SEEK_END)
        try:
            while True:
                line = handle.readline()
                if not line:
                    time.sleep(0.5)
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if not problems_only or _is_problem(entry):
                    _print_entry(entry)
        except KeyboardInterrupt:
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect API keepalive/session scheduler state and journal."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show persisted scheduler/cache state")
    sub.add_parser("live", help="query the running gateway (includes snapshot_ready)")
    tail = sub.add_parser("tail", help="show recent scheduler journal entries")
    tail.add_argument("-n", "--lines", type=int, default=30)
    errors = sub.add_parser("errors", help="show recent warnings/errors/misses")
    errors.add_argument("-n", "--lines", type=int, default=30)
    follow = sub.add_parser("follow", help="follow all new journal entries")
    follow.add_argument("--errors", action="store_true", help="only problems")
    args = parser.parse_args()
    if args.command == "status":
        return command_status()
    if args.command == "live":
        return command_live()
    if args.command == "tail":
        return command_tail(args.lines)
    if args.command == "errors":
        return command_tail(args.lines, problems_only=True)
    return command_follow(args.errors)


if __name__ == "__main__":
    raise SystemExit(main())
