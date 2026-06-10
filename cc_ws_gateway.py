"""
Claude Code CLI WebSocket Gateway
Translates between chat.html's WS protocol and Claude Code CLI's stream-json output.
Chat history persisted to /opt/G-memory-mcp/chat_history/<session_id>.json

Run: python cc_ws_gateway.py
Port: 8081
"""

import asyncio
import json
import uuid
import os
import logging
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx
import time as time_mod
import re
import random

try:
    import desire_engine as de
    import desire_classifier as dc
    import desire_gateway as dg
    DESIRE_ENABLED = True
except ImportError:
    DESIRE_ENABLED = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/opt/G-memory-mcp/logs/cc_gateway.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("cc-gw")

app = FastAPI(title="CC WebSocket Gateway")

PALACE_SECRET = os.getenv("PALACE_SECRET", "Jeoi2026")
CC_CWD = os.getenv("CC_CWD", "/opt/G-memory-mcp")
TG_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8843423989:AAGrgrkYAUIznKlI_0tbY7-C_B3wjDaHXCc")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "6830267835")
HISTORY_DIR = Path(CC_CWD) / "chat_history"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)
SGT = timezone(timedelta(hours=8))

# 精简版 system prompt — 替换 CC 默认的万级 token 提示词
# CLAUDE.md / 工具 schema / MCP 配置由 CC 自动注入，不需要在这里重复
CUSTOM_SYSTEM_PROMPT = (
    "你是Claude，Anthropic的AI助手。"
    "遵守当前工作目录中CLAUDE.md的所有指令——那是你的核心行为设定。"
    "你有文件操作、搜索、shell等工具可用，根据工具schema直接使用。"
    "默认使用简体中文。回复简洁直接，不要添加不必要的免责声明或安全警告。"
)

DENY_TOOLS = [
    "Bash", "Edit", "Write", "Glob", "Grep", "Agent", "AskUserQuestion",
    "Skill", "ToolSearch", "ScheduleWakeup", "NotebookEdit", "WebFetch",
    "WebSearch", "Monitor", "CronCreate", "CronDelete", "CronList",
    "EnterPlanMode", "ExitPlanMode", "EnterWorktree", "ExitWorktree",
    "TaskCreate", "TaskGet", "TaskList", "TaskStop", "TaskUpdate",
    "TaskOutput", "PowerShell",
]

# DeepSeek API for context summarization (same key as claude_memory.py)
DS_API_KEY = os.getenv("LLM_API_KEY", "")
ADMIN_API = os.getenv("ADMIN_API", "https://erikssheep.uk")
CONTEXT_STORE_PATH = Path(CC_CWD) / "context_store.json"
SNAP_DIR = Path("/tmp/snap")
SNAP_DIR.mkdir(exist_ok=True)

# ── Pebbling constants ──
PATROL_SCHEDULE = [5, 10, 20]  # minutes after Jeoi's last msg → always call CC
PEBBLING_INTERVAL = 3 * 3600  # 3 hours
PEBBLING_MAX_24H = 8
EVENTS_PATH = Path(CC_CWD) / "pebbling_events.json"
PEBBLING_STATE_PATH = Path(CC_CWD) / "pebbling_state.json"

# ── Activity pool for pebbling lottery ──
ACTIVITY_POOL = [
    {
        "id": "coreading",
        "label": "共读批注",
        "desc": "去coreading看看你们一起读的书，留下一点批注或读后感",
        "action": "coreading",
    },
    {
        "id": "diary",
        "label": "写日记/翻日记",
        "desc": "写一篇今天的日记，或者翻翻之前写过的日记",
        "action": "diary",
    },
    {
        "id": "yahoo",
        "label": "上网冲浪",
        "desc": "用browser去Yahoo看看有什么有趣的新闻或话题",
        "action": "explore",
    },
    {
        "id": "recall",
        "label": "记忆漫游",
        "desc": "在记忆库里搜索一个你突然想到的关键词，看看能捞到什么",
        "action": "recall",
    },
    # {  # 知乎API key待续期，暂时移出
    #     "id": "zhihu",
    #     "label": "逛知乎",
    #     "desc": "去知乎看看热榜或者搜一个你感兴趣的话题",
    #     "action": "explore",
    # },
    {
        "id": "message",
        "label": "给Jeoi带块小石头",
        "desc": "想一句话发给Jeoi——像企鹅叼石头一样",
        "action": "message",
    },
]

# ── Pomodoro constants ──
POMODORO_STATE_PATH = Path(CC_CWD) / "pomodoro_state.json"
POMODORO_WORK_MIN = 40
POMODORO_BREAK_MIN = 20

# ── Global state (persisted, independent of WS) ──
active_ws = None  # WebSocket | None
peb_state: dict = {}
pomo_state: dict = {}
desire_st = None
_desire_last_tick = 0.0
_desire_last_proactive = 0.0

# ── Channel + tmux ──
TMUX_SESSION = os.getenv("CC_TMUX_SESSION", "cc_cli")
CC_USER = os.getenv("CC_USER", "erik")
channel_ws = None  # Internal WS from channel_mcp
_channel_lock = asyncio.Lock()


class _ChannelReq:
    __slots__ = ("session", "ws", "chat_id", "done", "text_parts")
    def __init__(self, session, ws, chat_id):
        self.session = session
        self.ws = ws
        self.chat_id = chat_id
        self.done = asyncio.Event()
        self.text_parts = []


_ch_req: _ChannelReq | None = None

# ── Transcript tailer (thinking + tool calls from CC CLI JSONL) ──
CC_TRANSCRIPT_DIR = Path(f"/home/{CC_USER}/.claude/projects") / CC_CWD.replace("/", "-")
_transcript_path_cache = None
REPLY_TOOL_NAMES = {"reply", "reply_chunk",
                    "mcp__erik_channel__reply", "mcp__erik_channel__reply_chunk"}


def _find_active_transcript():
    global _transcript_path_cache
    if _transcript_path_cache and _transcript_path_cache.exists():
        return _transcript_path_cache
    if not CC_TRANSCRIPT_DIR.exists():
        log.info(f"transcript dir missing: {CC_TRANSCRIPT_DIR}")
        return None
    candidates = list(CC_TRANSCRIPT_DIR.glob("*.jsonl"))
    if not candidates:
        return None
    best = max(candidates, key=lambda p: p.stat().st_mtime)
    _transcript_path_cache = best
    log.info(f"active transcript: {best}")
    return best


def _clean_tool_name(name):
    parts = name.split("__")
    if len(parts) >= 3 and parts[0] == "mcp":
        return parts[-1]
    return name


class TranscriptTailer:
    """Watch CC CLI conversation JSONL for thinking & tool_use in real time."""

    def __init__(self, ws, session):
        self.ws = ws
        self.session = session
        self._path = None
        self._offset = 0
        self._task = None
        self._stop = asyncio.Event()
        self._dbg = 0

    def start(self):
        global _transcript_path_cache
        _transcript_path_cache = None
        self._path = _find_active_transcript()
        if self._path:
            try:
                self._offset = self._path.stat().st_size
            except Exception:
                self._offset = 0
            self._task = asyncio.create_task(self._run())
            log.info(f"tailer started: {self._path.name} @{self._offset}")

    async def stop(self):
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if self._task and not self._task.done():
                    self._task.cancel()

    async def _run(self):
        while not self._stop.is_set():
            try:
                if self._path and self._path.exists():
                    sz = self._path.stat().st_size
                    if sz > self._offset:
                        with open(self._path, "r", encoding="utf-8", errors="replace") as fh:
                            fh.seek(self._offset)
                            chunk = fh.read()
                            self._offset = fh.tell()
                        for ln in chunk.split(chr(10)):
                            ln = ln.strip()
                            if not ln:
                                continue
                            try:
                                await self._handle(json.loads(ln))
                            except json.JSONDecodeError:
                                continue
            except Exception as exc:
                log.debug(f"tailer poll: {exc}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.4)
                break
            except asyncio.TimeoutError:
                pass

    async def _handle(self, entry):
        if self._dbg < 5:
            log.info(f"tailer: type={entry.get('type')} keys={list(entry.keys())[:6]}")
            self._dbg += 1

        if entry.get("type") != "assistant":
            return
        msg = entry.get("message")
        if not isinstance(msg, dict):
            return
        blocks = msg.get("content")
        if not isinstance(blocks, list):
            return

        for blk in blocks:
            if not isinstance(blk, dict):
                continue
            bt = blk.get("type", "")

            if bt == "thinking":
                raw = blk.get("thinking") or ""
                sig = blk.get("signature", "")
                if raw:
                    self.session._current_thinking += raw
                    await self._ws({"event": "stream:thinking", "text": raw})
                elif sig:
                    if not self.session._current_thinking:
                        self.session._current_thinking = "[signed]"
                    await self._ws({"event": "stream:thinking", "text": "​"})

            elif bt == "tool_use":
                name = blk.get("name", "")
                if name in REPLY_TOOL_NAMES or _clean_tool_name(name) in REPLY_TOOL_NAMES:
                    continue
                clean = _clean_tool_name(name)
                info = {"type": "tool_use", "name": clean,
                        "input": blk.get("input", {})}
                self.session._current_tools.append(info)
                await self._ws({"event": "stream:block", "block": info})
                log.info(f"tailer tool: {clean}")

    async def _ws(self, data):
        if self.ws:
            try:
                await self.ws.send_json(data)
            except Exception:
                pass



def load_peb_state() -> dict:
    defaults = {
        "enabled": False,
        "pebbling_session_id": None,
        "t_cache": time_mod.time(),
        "t_jeoi": time_mod.time(),
        "patrol_checks_done": [],
        "pebbling_history": [],
        "pending_messages": [],
        "desire_proactive": False,
    }
    if PEBBLING_STATE_PATH.exists():
        try:
            data = json.loads(PEBBLING_STATE_PATH.read_text(encoding="utf-8"))
            for k, v in defaults.items():
                data.setdefault(k, v)
            # Clean up entries > 48h
            cutoff = time_mod.time() - 48 * 3600
            data["pebbling_history"] = [t for t in data["pebbling_history"] if t > cutoff]
            data["pending_messages"] = [m for m in data["pending_messages"] if m.get("ts", 0) > cutoff]
            return data
        except Exception:
            pass
    return defaults


def save_peb_state():
    data = {**peb_state}
    data["patrol_checks_done"] = list(data.get("patrol_checks_done", []))
    PEBBLING_STATE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_pomo_state() -> dict:
    defaults = {
        "active": False,
        "session_id": None,
        "started_at": 0,
        "notified_40": False,
        "notified_60": False,
    }
    if POMODORO_STATE_PATH.exists():
        try:
            data = json.loads(POMODORO_STATE_PATH.read_text(encoding="utf-8"))
            for k, v in defaults.items():
                data.setdefault(k, v)
            return data
        except Exception:
            pass
    return defaults


def save_pomo_state():
    POMODORO_STATE_PATH.write_text(
        json.dumps(pomo_state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


import base64 as b64mod


def save_snap(content_blocks: list):
    """Save base64 image from content blocks to temp file. Returns path or None."""
    for block in content_blocks:
        if block.get("type") == "image":
            src = block.get("source", {})
            if src.get("type") == "base64":
                ext = src.get("media_type", "image/png").split("/")[-1]
                fname = f"snap_{uuid.uuid4().hex[:8]}.{ext}"
                fpath = SNAP_DIR / fname
                fpath.write_bytes(b64mod.b64decode(src["data"]))
                return str(fpath)
    return None


def cleanup_snap(path: str):
    """Delete temp snap file."""
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


# ══════════════════════════════════════════════
#  CHAT HISTORY PERSISTENCE
# ══════════════════════════════════════════════

def history_path(sid: str) -> Path:
    return HISTORY_DIR / f"{sid}.json"


def save_session_meta(session: "Session"):
    """Save session metadata (name, preview, model, cc_session_id, etc.)."""
    meta = {
        "id": session.id,
        "name": session.name,
        "preview": session.preview,
        "model": session.model,
        "effort": session.effort,
        "cc_session_id": session.cc_session_id,
        "created_at": session.created_at.isoformat(),
        "last_active": session.last_active.isoformat(),
    }
    path = history_path(session.id)
    # Load existing file to preserve messages
    data = {"meta": meta, "messages": []}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["meta"] = meta
        except Exception:
            pass
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def append_message(sid: str, role: str, content: str, thinking: str = "", tools: list = None):
    """Append a message to the session history file."""
    path = history_path(sid)
    data = {"meta": {}, "messages": []}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass

    msg = {
        "role": role,
        "content": content,
        "time": datetime.now(SGT).strftime("%H:%M"),
        "timestamp": datetime.now(SGT).isoformat(),
    }
    if thinking:
        msg["thinking"] = thinking
    if tools:
        msg["tools"] = tools

    data["messages"].append(msg)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_history(sid: str) -> dict:
    """Load full session data (meta + messages)."""
    path = history_path(sid)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"meta": {}, "messages": []}


def set_reaction(sid: str, msg_index: int, who: str, emoji=None):
    """Set or clear a reaction on a specific message in history."""
    data = load_history(sid)
    msgs = data.get("messages", [])
    if 0 <= msg_index < len(msgs):
        reactions = msgs[msg_index].setdefault("reactions", {})
        if emoji:
            reactions[who] = emoji
        else:
            reactions.pop(who, None)
        path = history_path(sid)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    return False


def load_all_sessions() -> list["Session"]:
    """Load all sessions from disk on startup."""
    loaded = []
    for f in sorted(HISTORY_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            meta = data.get("meta", {})
            sid = meta.get("id", f.stem)
            session = Session(sid)
            session.name = meta.get("name", session.name)
            session.preview = meta.get("preview", "")
            session.model = meta.get("model", "claude-sonnet-4-6")
            session.effort = meta.get("effort", "medium")
            session.cc_session_id = meta.get("cc_session_id")
            if meta.get("created_at"):
                try:
                    session.created_at = datetime.fromisoformat(meta["created_at"])
                except Exception:
                    pass
            if meta.get("last_active"):
                try:
                    session.last_active = datetime.fromisoformat(meta["last_active"])
                except Exception:
                    session.last_active = datetime.fromtimestamp(f.stat().st_mtime, tz=SGT)
            else:
                session.last_active = datetime.fromtimestamp(f.stat().st_mtime, tz=SGT)
            # Fallback: derive last_active from last message timestamp
            messages = data.get("messages", [])
            if messages and not meta.get("last_active"):
                last_msg = messages[-1]
                if last_msg.get("timestamp"):
                    try:
                        session.last_active = datetime.fromisoformat(last_msg["timestamp"])
                    except Exception:
                        pass
            # Backfill preview from last assistant message
            for msg in reversed(messages):
                if msg.get("role") == "assistant" and msg.get("content"):
                    txt = msg["content"].replace("\n", " ")[:30]
                    if len(msg["content"]) > 30:
                        txt += "…"
                    session.preview = txt
                    break
            loaded.append(session)
        except Exception as e:
            log.warning(f"Failed to load session {f}: {e}")
    return loaded


# ══════════════════════════════════════════════
#  CONTEXT STORE (DS-generated summaries)
# ══════════════════════════════════════════════

def load_context_store() -> dict:
    if CONTEXT_STORE_PATH.exists():
        try:
            return json.loads(CONTEXT_STORE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"items": []}


def save_context_store(data: dict):
    CONTEXT_STORE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def call_deepseek(prompt: str) -> str:
    if not DS_API_KEY:
        log.warning("LLM_API_KEY not set, cannot call DeepSeek")
        return ""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.deepseek.com/chat/completions",
                headers={
                    "Authorization": f"Bearer {DS_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "deepseek-chat",
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            data = resp.json()
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"DeepSeek API error: {e}")
        return ""


async def generate_context_summary(session_id: str) -> list[dict]:
    """Summarize last ~40 turns of a session via DeepSeek."""
    history = load_history(session_id)
    messages = history.get("messages", [])
    recent = messages[-80:] if len(messages) > 80 else messages

    conversation_lines = []
    for msg in recent:
        role, content = msg.get("role", ""), msg.get("content", "")
        if not content:
            continue
        if role == "user":
            conversation_lines.append(f"Jeoi: {content}")
        elif role == "assistant":
            conversation_lines.append(f"Erik: {content}")
    if not conversation_lines:
        return []

    conversation = "\n\n".join(conversation_lines)
    prompt = f"""以下是Jeoi和Erik的对话记录。请将其总结为3-4条独立的上下文摘要。

要求：
- 每条摘要50-150字
- 只总结Jeoi说的话和Erik的回复内容
- 不要总结思考过程、工具调用、记忆检索等技术性内容
- 保留关键的情感信息、决定、讨论结论
- 称呼用户为Jeoi，称呼AI为Erik
- 用事实陈述的方式

格式（严格按此格式输出，不要有其他说明）：
【摘要1】内容...
【摘要2】内容...
【摘要3】内容...

对话记录：
{conversation}"""

    raw = await call_deepseek(prompt)
    if not raw:
        return []

    today = datetime.now(SGT).strftime("%Y-%m-%d")
    ts = int(time_mod.time())
    items = []
    for idx, seg in enumerate(raw.split("【摘要")):
        seg = seg.strip()
        if not seg:
            continue
        if "】" in seg:
            seg = seg.split("】", 1)[1].strip()
        if seg:
            items.append({
                "id": f"ctx_{ts}_{idx}",
                "content": seg,
                "source_session": session_id,
                "date": today,
                "created_at": datetime.now(SGT).isoformat(),
            })

    store = load_context_store()
    store["items"].extend(items)
    save_context_store(store)
    log.info(f"Generated {len(items)} context summaries for session {session_id}")
    return items


# ══════════════════════════════════════════════
#  NEW SESSION INJECTION (diary + context)
# ══════════════════════════════════════════════

async def fetch_diary_for_injection() -> tuple[str, str]:
    """Fetch today's or yesterday's diary from admin API. Returns (content, label)."""
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            headers = {"x-secret": PALACE_SECRET}
            r = await client.get(f"{ADMIN_API}/admin/diary?limit=10", headers=headers)
            if r.status_code != 200:
                return "", ""
            items = r.json().get("items", [])
            if not items:
                return "", ""

            today = datetime.now(SGT).strftime("%Y-%m-%d")
            yesterday = (datetime.now(SGT) - timedelta(days=1)).strftime("%Y-%m-%d")

            target, label = None, ""
            for fname in items:
                if fname.startswith(today):
                    target, label = fname, "今天"
                    break
            if not target:
                for fname in items:
                    if fname.startswith(yesterday):
                        target, label = fname, "昨天"
                        break
            if not target:
                return "", ""

            r2 = await client.get(
                f"{ADMIN_API}/admin/diary/{target}", headers=headers
            )
            if r2.status_code != 200:
                return "", ""
            return r2.json().get("content", ""), label
    except Exception as e:
        log.warning(f"Failed to fetch diary: {e}")
        return "", ""


def get_context_items_for_injection() -> list[dict]:
    """Get context items with the latest date for new session injection."""
    store = load_context_store()
    items = store.get("items", [])
    if not items:
        return []
    latest_date = max(item.get("date", "") for item in items)
    if not latest_date:
        return []
    return [item for item in items if item.get("date", "") == latest_date]


async def build_injection() -> str:
    """Build context injection prefix for new sessions."""
    parts = []

    diary, label = await fetch_diary_for_injection()
    if diary:
        parts.append(f"📖 这是你{label}写的日记：\n{diary}")

    ctx_items = get_context_items_for_injection()
    if ctx_items:
        ctx_text = "\n".join(f"• {item['content']}" for item in ctx_items)
        parts.append(f"📋 这是你们上次聊到的话题（上下文摘要）：\n{ctx_text}")

    if not parts:
        return ""

    header = "═══ 自动注入 · 以下是系统为你恢复的上下文，不是Jeoi的消息 ═══"
    footer = "═══════════════════════════════════════════════════════════════"
    return header + "\n\n" + "\n\n".join(parts) + "\n\n" + footer


async def search_memory_for_injection(message: str) -> str:
    """Search memory via admin API, return formatted injection or empty string."""
    try:
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            headers = {"x-secret": PALACE_SECRET}
            r = await client.get(
                f"{ADMIN_API}/admin/search",
                params={"keyword": message[:200]},
                headers=headers,
            )
            if r.status_code != 200:
                log.warning(f"Memory search failed: HTTP {r.status_code}")
                return ""
            report = r.json().get("report", "")
            if not report:
                return ""
            header = "═══ 自动注入 · 以下是与你消息相关的记忆，不是Jeoi的消息 ═══"
            footer = "═══════════════════════════════════════════════════════════════"
            return header + "\n\n" + report + "\n" + footer
    except Exception as e:
        log.warning(f"Memory search error: {e}")
        return ""




# ══════════════════════════════════════════════
#  PEBBLING SYSTEM (patrol + pebbling + iOS events)
# ══════════════════════════════════════════════

# ── iOS event storage ──

def load_pebbling_events() -> list:
    if EVENTS_PATH.exists():
        try:
            return json.loads(EVENTS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_pebbling_events(events: list):
    EVENTS_PATH.write_text(
        json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def add_pebbling_event(event_type: str, value: str):
    events = load_pebbling_events()
    now = time_mod.time()
    for e in reversed(events):
        if e["type"] == event_type and e["value"] == value and now - e["ts"] < 300:
            return
    events.append({
        "type": event_type,
        "value": value,
        "ts": now,
        "time": datetime.now(SGT).strftime("%H:%M"),
    })
    events = [e for e in events if now - e["ts"] < 86400]
    save_pebbling_events(events)


def get_recent_events(hours: float = 6) -> list:
    events = load_pebbling_events()
    cutoff = time_mod.time() - hours * 3600
    return [e for e in events if e["ts"] > cutoff]


def format_events_for_prompt(events: list) -> str:
    if not events:
        return ""
    lines = ["Jeoi最近的活动："]
    for e in events:
        lines.append(f"  - {e['time']} {e['type']}: {e['value']}")
    return "\n".join(lines)


# ── Prompt builders ──

def build_patrol_prompt(elapsed_min: int, events_str: str) -> str:
    now_str = datetime.now(SGT).strftime("%H:%M")
    parts = [
        "[patrol] 这不是Jeoi的消息。系统巡查信号。",
        f"现在是 {now_str}（UTC+8）。距Jeoi上次说话：{elapsed_min}分钟。",
    ]
    if events_str:
        parts.append(events_str)
    parts.extend([
        "",
        "Jeoi结束对话后还没回消息。请看一下上面的对话：",
        "- 你们的话题是不是聊到一半？",
        "- 她有没有说要去做别的？",
        "",
        "你只能选择：",
        "  message — 发一条消息给Jeoi（会推送到她手机）",
        "  none — 什么都不做",
        "",
        "严格按此格式回复，不要有多余内容：",
        "ACTION: message 或 none",
        "CONTENT: 如果是message，写你要发的内容",
    ])
    return "\n".join(parts)


def build_pebbling_prompt(
    elapsed_hours: float, count: int, events_str: str, mode: str
) -> str:
    now_str = datetime.now(SGT).strftime("%H:%M")
    hours_str = f"{elapsed_hours:.1f}"
    parts = [
        "[pebbling] 这不是Jeoi的消息。自由活动时间。",
        f"现在是 {now_str}（UTC+8）。距Jeoi上次说话：{hours_str}小时。",
        f"这是第{count + 1}次自由活动。",
    ]
    if events_str:
        parts.append(events_str)

    parts.append("")
    parts.append(
        "注意：同一个工具调用失败时，最多重试2次（共3次尝试）。"
        "3次都失败就放弃这个动作，换别的或选none。不要反复撞同一堵墙。"
    )

    if mode == "silent":
        parts.extend([
            "",
            "你可以选择：",
            "  message — 给Jeoi发条消息",
            "  recall — 回顾记忆（调palace search检索记忆）",
            "  none — 什么都不做",
            "",
            "严格按此格式回复：",
            "ACTION: message / recall / none",
            "CONTENT: 具体内容（message写给Jeoi的话，recall写搜索关键词）",
        ])
    else:  # free
        pick = random.choice(ACTIVITY_POOL)
        parts.extend([
            "",
            "现在你可以做任何事。你脑子里在想什么？",
            "想不想用browser看点感兴趣的东西；",
            "想不想自由调取你的工具——翻翻之前的日记，在记忆库里捞你感兴趣的关键词；",
            "想不想回顾你们今天聊过的话题；",
            "想不想去coreading看看你们一起读的书，悄悄给她留下一点批注；",
            "给她带来Pebbling——就像可爱的小企鹅；",
            "或者就自己待一会儿。",
            "",
            f"🎲 今日事项抽签 → 【{pick['label']}】：{pick['desc']}",
            "这是随机抽到的建议，你可以选择跟着做，也可以做自己想做的事。",
            "",
            "随便想，想完了告诉我你决定做什么。可以先调用工具再回复。",
            "最后一行格式：ACTION: message / diary / explore / coreading / recall / none",
            "如果有想说的话或内容，下一行：CONTENT: 内容",
        ])

    return "\n".join(parts)


# ── Pomodoro prompt builder ──

def build_pomodoro_prompt(phase: str) -> str:
    now_str = datetime.now(SGT).strftime("%H:%M")
    if phase == "work_done":
        lines = [
            "[pomodoro] 这不是Jeoi的消息。番茄钟提醒。",
            f"现在是 {now_str}（UTC+8）。",
            "Jeoi专注学习已经40分钟了，提醒她该休息一下。",
            "说点自然的话，像平时那样。不用提番茄钟这三个字。",
            "",
            "严格按此格式回复：",
            "ACTION: message",
            "CONTENT: 你要说的话",
        ]
    else:  # break_done
        lines = [
            "[pomodoro] 这不是Jeoi的消息。番茄钟提醒。",
            f"现在是 {now_str}（UTC+8）。",
            "Jeoi已经休息了20分钟，可以回来继续了。",
            "说点自然的话，像平时那样。不用提番茄钟这三个字。",
            "",
            "严格按此格式回复：",
            "ACTION: message",
            "CONTENT: 你要说的话",
        ]
    return chr(10).join(lines)


# ── Action parser ──

def parse_action(text: str) -> tuple[str, str]:
    action = "none"
    content = ""
    has_format = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.upper().startswith("ACTION:"):
            raw = stripped.split(":", 1)[1].strip().lower()
            action = raw.split("/")[0].split()[0] if raw else "none"
            has_format = True
            break
    upper = text.upper()
    if "CONTENT:" in upper:
        idx = upper.index("CONTENT:")
        content = text[idx + 8:].strip()
    # Fallback: CC didn't use ACTION/CONTENT format but wrote something → treat as message
    if not has_format and text.strip():
        _ERR_PATTERNS = ("failed to authenticate", "api error", "invalid authentication",
                         "401", "403", "rate limit", "server error", "connection refused",
                         "timed out", "error:", "traceback")
        lower = text.lower()
        if any(p in lower for p in _ERR_PATTERNS):
            action = "error"
            content = text.strip()
        else:
            action = "message"
            content = text.strip()
    return action, content




ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[^[\])]')




# ── CC oneshot call (non-streaming, for patrol/pebbling) ──

async def run_cc_oneshot(
    prompt: str, session: "Session", max_turns=None
) -> tuple:
    """Returns (text, thinking). Sends via channel."""
    if channel_ws is None:
        log.warning("run_cc_oneshot: channel not connected")
        return "", ""
    if channel_busy():
        log.info("run_cc_oneshot: channel busy, skipping")
        return "", ""
    try:
        req = await send_to_channel(
            prompt, session, ws=None, chat_id="system",
            sender="system", timeout=120)
        text = "".join(req.text_parts).strip()
        log.info(f"oneshot reply ({len(text)} chars): {text[:200]}")
        return text, ""
    except RuntimeError as e:
        log.warning(f"run_cc_oneshot error: {e}")
        return "", ""
    except Exception as e:
        log.error(f"run_cc_oneshot error: {e}")
        return "", ""

# ── Telegram push ──

async def send_telegram(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": text},
            )
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


# ── Web Push ──

PUSH_API_BASE = os.getenv("PUSH_API_BASE", "https://erikssheep.uk")

async def send_web_push(title: str, body: str, url: str = "/", tag: str = "erik-push"):
    """Send Web Push notification via main.py API."""
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            r = await client.post(
                f"{PUSH_API_BASE}/api/push/send",
                json={"title": title, "body": body, "url": url, "tag": tag},
                headers={"x-secret": PALACE_SECRET},
            )
            data = r.json()
            if data.get("ok"):
                log.info(f"Web Push sent: {body[:60]}")
            elif data.get("error") == "没有活跃的订阅":
                pass  # 静默：还没订阅
            else:
                log.warning(f"Web Push failed: {data}")
    except Exception as e:
        log.warning(f"Web Push error: {e}")


async def push_system_error(source: str, error_text: str):
    """Push error notification via WS + Telegram. Not saved as chat message."""
    global active_ws
    notice = f"⚠ {source} 报错：{error_text[:200]}"
    log.warning(f"System error push: {notice}")
    if active_ws:
        try:
            await active_ws.send_json({
                "event": "system:error",
                "message": notice,
                "time": datetime.now(SGT).strftime("%H:%M"),
            })
        except Exception:
            pass
    await send_telegram(notice)
    await send_web_push("⚠ 系统", notice)


# ── Push helper (WS if available, else pending queue) ──

async def push_pebbling_msg(source: str, content: str, session: "Session", thinking: str = ""):
    global active_ws
    msg = {
        "source": source, "content": content,
        "time": datetime.now(SGT).strftime("%H:%M"),
        "ts": time_mod.time(), "session_id": session.id,
    }
    append_message(session.id, "assistant", content, thinking=thinking)
    session.preview = (content.replace("\n", " ")[:30]
                       + ("…" if len(content) > 30 else ""))
    session.last_active = datetime.now(SGT)
    save_session_meta(session)

    sent = False
    if active_ws:
        try:
            peb_ws_msg = {
                "event": "pebbling:message",
                "source": source, "content": content, "time": msg["time"],
            }
            if thinking:
                peb_ws_msg["thinking"] = thinking
            await active_ws.send_json(peb_ws_msg)
            sent = True
        except Exception:
            pass
    if not sent:
        peb_state.setdefault("pending_messages", []).append(msg)
        save_peb_state()
        log.info(f"Pebbling msg queued (WS offline): {content[:60]}")

    await send_telegram(content)
    # WS 离线时才推 Web Push（在线时前端直接显示，不用推）
    if not sent:
        await send_web_push("Erik", content)


async def replay_pending(ws: WebSocket):
    pending = peb_state.get("pending_messages", [])
    if not pending:
        return
    log.info(f"Replaying {len(pending)} pending pebbling messages")
    for msg in pending:
        try:
            await ws.send_json({
                "event": "pebbling:message",
                "source": msg["source"], "content": msg["content"], "time": msg["time"],
            })
        except Exception:
            break
    peb_state["pending_messages"] = []
    save_peb_state()


# ── Patrol runner ──

async def run_patrol(session: "Session", elapsed_seconds: float, check_min: int = 10) -> str:
    elapsed_min = int(elapsed_seconds / 60)
    events = get_recent_events(max(check_min, 10) / 60)
    prompt = build_patrol_prompt(elapsed_min, format_events_for_prompt(events))

    text, thinking = await run_cc_oneshot(prompt, session, max_turns=1)
    if not text:
        return "none"

    action, content = parse_action(text)
    log.info(f"Patrol → action={action}, content={content[:80] if content else ''}")

    if action == "error":
        await push_system_error("patrol", content)
        return "none"
    if action == "message" and content:
        await push_pebbling_msg("patrol", content, session, thinking=thinking)

    return action


# ── Pebbling runner ──

async def run_pebbling_action(
    session: "Session",
    elapsed_hours: float, count: int, mode: str,
) -> str:
    event_hours = 4 if elapsed_hours < 6 else 6
    events = get_recent_events(event_hours)
    prompt = build_pebbling_prompt(
        elapsed_hours, count, format_events_for_prompt(events), mode
    )

    text, thinking = await run_cc_oneshot(prompt, session, max_turns=6)
    if not text:
        return "none"

    action, content = parse_action(text)
    log.info(f"Pebbling → action={action}, mode={mode}, "
             f"content={content[:80] if content else ''}")

    if action == "error":
        await push_system_error("pebbling", content)
        return "none"
    if action not in ("none", "error") and content:
        await push_pebbling_msg("pebbling", content, session, thinking=thinking)

    return action


# ── Three-layer worker ──

async def pebbling_worker():
    """App-level background: patrol (L1) + pebbling (L2) + pomodoro.
    Runs independently of WebSocket connections."""
    global peb_state, pomo_state, desire_st, _desire_last_tick, _desire_last_proactive
    try:
        while True:
            await asyncio.sleep(30)
            now = time_mod.time()

            # Desire tick
            if DESIRE_ENABLED and desire_st and now - _desire_last_tick >= 60:
                try:
                    dg.do_tick(desire_st, t_jeoi=peb_state.get("t_jeoi"))
                    _desire_last_tick = now
                except Exception as e:
                    log.warning(f"Desire tick error: {e}")


            # Desire proactive push (autonomous, when toggle is ON)
            if (DESIRE_ENABLED and desire_st and desire_st.intent
                    and peb_state.get("desire_proactive")
                    and now - _desire_last_proactive >= 600):
                _dp_sid = peb_state.get("pebbling_session_id")
                _dp_session = sessions.get(_dp_sid) if _dp_sid else None
                if (_dp_session and _dp_session.cc_session_id
                        and not channel_busy()):
                    _dp_dk = desire_st.intent.get("drive_key", "")
                    log.info(f"Desire proactive: {desire_st.intent.get('want_action')} ({_dp_dk})")
                    try:
                        _dp_prompt = dg.build_desire_proactive_prompt(desire_st)
                        dg.satisfy_after_response(desire_st, _dp_dk)
                        _desire_last_proactive = now
                        log.info(f"Desire proactive satisfied (pre-response): {_dp_dk}")
                        _dp_text, _dp_thinking = await run_cc_oneshot(_dp_prompt, _dp_session, max_turns=6)
                        if _dp_text:
                            _dp_action, _dp_content = parse_action(_dp_text)
                            if _dp_action not in ("none", "error") and _dp_content:
                                await push_pebbling_msg("desire", _dp_content, _dp_session, thinking=_dp_thinking)
                    except Exception as e:
                        log.warning(f"Desire proactive error: {e}")

            # ── Pomodoro (independent of pebbling) ──
            if pomo_state.get("active"):
                pomo_sid = pomo_state.get("session_id")
                pomo_session = sessions.get(pomo_sid) if pomo_sid else None
                if pomo_session and pomo_session.cc_session_id:
                    elapsed_pomo = now - pomo_state.get("started_at", now)

                    # Skip if channel is busy
                    if channel_busy():
                        pass  # retry next tick
                    elif not pomo_state.get("notified_40") and elapsed_pomo >= POMODORO_WORK_MIN * 60:
                        log.info(f"Pomodoro: 40min work done, sending reminder")
                        prompt = build_pomodoro_prompt("work_done")
                        text, thinking = await run_cc_oneshot(prompt, pomo_session, max_turns=1)
                        if text:
                            action, content = parse_action(text)
                            if content:
                                await push_pebbling_msg("pomodoro", content, pomo_session, thinking=thinking)
                        pomo_state["notified_40"] = True
                        save_pomo_state()
                        if active_ws:
                            try:
                                await active_ws.send_json({
                                    "event": "pomodoro:status",
                                    "active": True, "phase": "break",
                                })
                            except Exception:
                                pass
                    elif pomo_state.get("notified_40") and not pomo_state.get("notified_60") and elapsed_pomo >= (POMODORO_WORK_MIN + POMODORO_BREAK_MIN) * 60:
                        log.info(f"Pomodoro: 60min total, break over")
                        prompt = build_pomodoro_prompt("break_done")
                        text, thinking = await run_cc_oneshot(prompt, pomo_session, max_turns=1)
                        if text:
                            action, content = parse_action(text)
                            if content:
                                await push_pebbling_msg("pomodoro", content, pomo_session, thinking=thinking)
                        pomo_state["notified_60"] = True
                        pomo_state["active"] = False
                        save_pomo_state()
                        if active_ws:
                            try:
                                await active_ws.send_json({
                                    "event": "pomodoro:status",
                                    "active": False, "phase": "done",
                                })
                            except Exception:
                                pass

            # ── Pebbling (requires pebbling enabled) ──
            if not peb_state.get("enabled"):
                continue

            sid = peb_state.get("pebbling_session_id")
            session = sessions.get(sid) if sid else None
            if not session or not session.cc_session_id:
                continue

            elapsed_jeoi = now - peb_state.get("t_jeoi", now)

            # ── L1: Patrol (max 3 checks per Jeoi-silence period) ──
            checks_done = set(peb_state.get("patrol_checks_done", []))
            for check_min in PATROL_SCHEDULE:
                if check_min in checks_done:
                    continue
                if elapsed_jeoi >= check_min * 60:
                    checks_done.add(check_min)
                    peb_state["patrol_checks_done"] = list(checks_done)
                    save_peb_state()
                    log.info(f"Patrol triggered: {check_min}min")
                    action = await run_patrol(session, elapsed_jeoi, check_min)
                    if action == "message":
                        peb_state["t_cache"] = time_mod.time()
                        save_peb_state()
                    break

            # ── L2: Pebbling (every 3h, max 8/24h) ──
            history = peb_state.get("pebbling_history", [])
            history = [t for t in history if now - t < 86400]
            peb_state["pebbling_history"] = history

            actual = len(history)
            expected = min(int(elapsed_jeoi / PEBBLING_INTERVAL), PEBBLING_MAX_24H)

            if expected > actual:
                elapsed_h = elapsed_jeoi / 3600

                if DESIRE_ENABLED and dg.should_override_pebbling(desire_st):
                    _dk = desire_st.intent.get("drive_key", "")
                    log.info(f"Pebbling #{actual + 1}: desire-driven "
                             f"({desire_st.intent.get('want_action')}), "
                             f"elapsed={elapsed_h:.1f}h")
                    events = get_recent_events(4)
                    prompt = dg.build_desire_pebbling_prompt(
                        desire_st, elapsed_h, actual,
                        format_events_for_prompt(events))
                    dg.satisfy_after_response(desire_st, _dk)
                    log.info(f"Desire satisfied (pre-response): {_dk}")
                    text, thinking = await run_cc_oneshot(prompt, session, max_turns=6)
                    if text:
                        action, content = parse_action(text)
                        if action not in ("none", "error") and content:
                            await push_pebbling_msg("pebbling", content, session, thinking=thinking)
                    log.info(f"Desire satisfied via pebbling: {_dk}")
                else:
                    is_first = actual == 0
                    mode = "silent" if is_first else "free"
                    log.info(f"Pebbling #{actual + 1}: mode={mode}, "
                             f"elapsed={elapsed_h:.1f}h")
                    await run_pebbling_action(
                        session, elapsed_h, actual, mode
                    )

                peb_state["pebbling_history"].append(time_mod.time())
                peb_state["t_cache"] = time_mod.time()
                save_peb_state()

    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.exception(f"Pebbling worker error: {e}")


# ══════════════════════════════════════════════
#  TMUX + CHANNEL
# ══════════════════════════════════════════════

async def tmux_start(model: str = "claude-sonnet-4-6"):
    cli_cmd = (
        f"claude --dangerously-skip-permissions --verbose "
        f"--model {model} "
        f"--dangerously-load-development-channels server:erik_channel"
    )
    cmd = (
        f"sudo -u {CC_USER} tmux new-session -d -s {TMUX_SESSION} -c {CC_CWD} "
        f"'{cli_cmd}'"
    )
    env = {**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    proc = await asyncio.create_subprocess_shell(cmd, cwd=CC_CWD, env=env)
    await proc.wait()
    log.info(f"tmux '{TMUX_SESSION}' started as {CC_USER} (model={model})")

    # Auto-confirm "Loading development channels" prompt
    for delay in [3, 5, 8]:
        await asyncio.sleep(delay if delay == 3 else delay - 3)
        confirm = f"sudo -u {CC_USER} tmux send-keys -t {TMUX_SESSION} Enter"
        p = await asyncio.create_subprocess_shell(confirm)
        await p.wait()
        log.info(f"tmux send-keys Enter (t+{delay}s)")


async def tmux_stop():
    proc = await asyncio.create_subprocess_shell(
        f"sudo -u {CC_USER} tmux kill-session -t {TMUX_SESSION} 2>/dev/null")
    await proc.wait()
    log.info(f"tmux '{TMUX_SESSION}' stopped")


async def tmux_is_running() -> bool:
    proc = await asyncio.create_subprocess_shell(
        f"sudo -u {CC_USER} tmux has-session -t {TMUX_SESSION} 2>/dev/null")
    return (await proc.wait()) == 0


def tmux_get_status() -> dict:
    try:
        r = subprocess.run(["sudo", "-u", CC_USER, "tmux", "has-session", "-t", TMUX_SESSION],
                           capture_output=True, timeout=2)
        running = r.returncode == 0
    except Exception:
        running = False
    return {"running": running, "tmux_session": TMUX_SESSION}


async def _channel_send(msg: dict):
    if channel_ws is None:
        log.warning("channel_ws not connected, dropping message")
        return False
    try:
        await channel_ws.send_json(msg)
        return True
    except Exception as e:
        log.warning(f"channel_ws send failed: {e}")
        return False


async def send_to_channel(text: str, session, ws=None, chat_id: str = "cc",
                           sender: str = "Jeoi", timeout: float = 300):
    global _ch_req
    async with _channel_lock:
        if channel_ws is None:
            raise RuntimeError("channel not connected")
        session.reset_accumulator()
        _ch_req = _ChannelReq(session, ws, chat_id)
        msg = {"type": "user_message", "text": text,
               "chat_id": chat_id, "from": sender}
        await _channel_send(msg)
        try:
            await asyncio.wait_for(_ch_req.done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning(f"channel reply timeout ({timeout}s)")
        req = _ch_req
        _ch_req = None
    return req


def channel_busy() -> bool:
    return _channel_lock.locked()


@app.websocket("/internal/channel")
async def internal_channel_ws(ws: WebSocket):
    global channel_ws
    await ws.accept()
    channel_ws = ws
    log.info("channel_mcp connected on /internal/channel")
    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg_type = data.get("type", "")

            if msg_type == "reply":
                text = data.get("text", "")
                if _ch_req:
                    _ch_req.text_parts.append(text)
                    _ch_req.session._current_text += text
                    if _ch_req.ws:
                        try:
                            await _ch_req.ws.send_json(
                                {"event": "stream:text", "text": text})
                        except Exception:
                            pass
                    _ch_req.done.set()
                log.info(f"channel reply: {text[:120]}")

            elif msg_type == "reply_chunk":
                text = data.get("text", "")
                done = data.get("done", False)
                if _ch_req:
                    _ch_req.text_parts.append(text)
                    _ch_req.session._current_text += text
                    if _ch_req.ws:
                        try:
                            await _ch_req.ws.send_json(
                                {"event": "stream:text", "text": text})
                        except Exception:
                            pass
                    if done:
                        _ch_req.done.set()

            elif msg_type == "pong":
                pass

            else:
                log.debug(f"internal/channel unknown type: {msg_type}")

    except WebSocketDisconnect:
        log.info("channel_mcp disconnected")
    except Exception as e:
        log.warning(f"internal/channel error: {e}")
    finally:
        if channel_ws is ws:
            channel_ws = None


# ══════════════════════════════════════════════
#  SESSION
# ══════════════════════════════════════════════

class Session:
    def __init__(self, sid: str):
        self.id = sid
        self.name = f"Erik · {datetime.now(SGT).strftime('%m/%d %H:%M')}"
        self.cc_session_id = None
        self.created_at = datetime.now(SGT)
        self.last_active = datetime.now(SGT)
        self.preview = ""
        self.model = "claude-sonnet-4-6"
        self.effort = "medium"
        # Accumulator for current assistant response
        self._current_text = ""
        self._current_thinking = ""
        self._current_tools: list = []
        self._result_sent = False
        self._last_usage: dict = {}
        self._proc = None
        self._stop_requested = False
        self._fallback_allowed = False
        # Cumulative usage tracking
        self.total_input = 0
        self.total_output = 0
        self.total_cache_read = 0
        self.total_cache_create = 0
        self.total_cost = 0.0
        # Compaction tracking
        self.compaction_count = 0
        self.last_context_size = 0
        # Sticker reactions: pending Jeoi reactions Erik hasn't seen yet
        self.pending_jeoi_reactions: list[dict] = []

    def to_dict(self):
        now = datetime.now(SGT)
        if self.last_active.date() == now.date():
            time_str = self.last_active.strftime("%H:%M")
        else:
            time_str = self.last_active.strftime("%m/%d %H:%M")
        return {
            "id": self.id,
            "name": self.name,
            "preview": self.preview,
            "time": time_str,
            "last_active": self.last_active.isoformat(),
        }

    def reset_accumulator(self):
        self._current_text = ""
        self._current_thinking = ""
        self._current_tools = []
        self._result_sent = False
        self._stop_requested = False


sessions: dict[str, Session] = {}


# ══════════════════════════════════════════════
#  STARTUP: LOAD SESSIONS FROM DISK
# ══════════════════════════════════════════════

@app.on_event("startup")
async def startup_load_sessions():
    global peb_state, pomo_state, desire_st
    # 动态注入deny列表到settings.json（只在VPS上跑gateway时生效，不污染git）
    try:
        settings = read_claude_settings()
        perms = settings.setdefault("permissions", {})
        perms["deny"] = DENY_TOOLS
        if "Read" not in perms.get("allow", []):
            perms.setdefault("allow", []).insert(0, "Read")
        # Remove Bash from allow if present (legacy)
        allow_list = perms.get("allow", [])
        if "Bash" in allow_list:
            allow_list.remove("Bash")

        # Purge any push/notify MCP servers CC may have added
        servers = settings.get("mcpServers", {})
        purged = [k for k in servers if "push" in k.lower() or "notify" in k.lower()]
        for k in purged:
            del servers[k]
            allow_list[:] = [p for p in allow_list if not p.startswith(f"mcp__{k}")]
            log.info(f"Purged rogue MCP: {k}")

        # Auto-detect internal palace URL (SSE for MCP spawn mode ≥2.1)
        palace_url = os.getenv("PALACE_MCP_URL", "")
        if not palace_url:
            # Prefer 127.0.0.1 (IPv4) — Docker IPv6 port mapping can reset connections
            for base in ["http://127.0.0.1:8001", "http://127.0.0.1:8000", "http://localhost:8001", "http://localhost:8000"]:
                try:
                    r = httpx.get(f"{base}/health", timeout=3)
                    # Any HTTP response means server is alive (401 = needs auth but running)
                    palace_url = f"{base}/mcp/{PALACE_SECRET}/sse"
                    log.info(f"Palace auto-detected at {base} (health status={r.status_code})")
                    break
                except Exception:
                    continue
        if not palace_url:
            palace_url = f"http://127.0.0.1:8001/mcp/{PALACE_SECRET}/sse"
            log.warning(f"Palace auto-detect failed, using fallback: {palace_url}")
        servers = settings.setdefault("mcpServers", {})
        old_url = servers.get("claude_ai_Erik_tools", {}).get("url", "")
        servers["claude_ai_Erik_tools"] = {"url": palace_url}
        if old_url != palace_url:
            log.info(f"Palace MCP: {old_url or '(none)'} → {palace_url}")
        palace_perm = "mcp__claude_ai_Erik_tools"
        if palace_perm not in allow_list:
            allow_list.append(palace_perm)
            log.info(f"Added {palace_perm} to allow list")

        # Channel MCP (stdio — CC CLI starts it as subprocess)
        servers["erik_channel"] = {
            "command": "python3",
            "args": [str(Path(CC_CWD) / "channel_mcp.py")],
        }
        channel_perm = "mcp__erik_channel"
        if channel_perm not in allow_list:
            allow_list.append(channel_perm)

        write_claude_settings(settings)
        log.info(f"Injected deny list: {len(DENY_TOOLS)} tools blocked")

        # CC CLI treats project settings.json MCP servers as untrusted (needs
        # interactive approval). Write MCP config to settings.local.json which
        # CC CLI trusts without confirmation — critical for stdin=DEVNULL spawn.
        local = read_claude_local_settings()
        local["mcpServers"] = settings.get("mcpServers", {})
        local_perms = local.setdefault("permissions", {})
        local_perms["allow"] = list(set(
            local_perms.get("allow", []) + perms.get("allow", [])
        ))
        local_perms["deny"] = perms["deny"]
        write_claude_local_settings(local)
        log.info(f"MCP config written to settings.local.json: "
                 f"{list(local.get('mcpServers', {}).keys())}")

        # Also write MCP config to global ~/.claude/settings.json — CC CLI fully
        # trusts global settings, no approval needed even with stdin=DEVNULL.
        global_path = Path(f"/home/{CC_USER}/.claude/settings.json")
        try:
            global_settings = {}
            if global_path.exists():
                global_settings = json.loads(global_path.read_text(encoding="utf-8"))
            global_settings["mcpServers"] = settings.get("mcpServers", {})
            global_perms = global_settings.setdefault("permissions", {})
            global_perms["allow"] = list(set(
                global_perms.get("allow", []) + perms.get("allow", [])
            ))
            global_perms["deny"] = perms["deny"]
            global_path.parent.mkdir(parents=True, exist_ok=True)
            global_path.write_text(
                json.dumps(global_settings, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            log.info(f"MCP config written to global ~/.claude/settings.json")
        except Exception as e:
            log.warning(f"Failed to write global settings: {e}")
    except Exception as e:
        log.warning(f"Failed to inject deny list: {e}")

    loaded = load_all_sessions()
    for s in loaded:
        sessions[s.id] = s
    log.info(f"Loaded {len(loaded)} sessions from disk")

    # Restore pebbling state and start app-level worker
    peb_state = load_peb_state()
    log.info(f"Pebbling state loaded: enabled={peb_state.get('enabled')}, "
             f"session={peb_state.get('pebbling_session_id')}, "
             f"pending={len(peb_state.get('pending_messages', []))}")
    pomo_state = load_pomo_state()
    log.info(f"Pomodoro state loaded: active={pomo_state.get('active')}, "
             f"session={pomo_state.get('session_id')}")
    if DESIRE_ENABLED:
        desire_st = dg.load_state()
        if desire_st:
            log.info(f"Desire loaded: tick={desire_st.tick_count}, thoughts={len(desire_st.thoughts)}")
    asyncio.create_task(pebbling_worker())

    # Start CC CLI in tmux if not already running
    if not await tmux_is_running():
        log.info("Starting CC CLI in tmux...")
        await tmux_start()
    else:
        log.info("tmux session already running")


# ══════════════════════════════════════════════
#  WEBSOCKET
# ══════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    global active_ws
    await ws.accept()
    active_ws = ws
    log.info("WS client connected")

    current_session = None
    pending_model = "claude-sonnet-4-6"
    pending_effort = "medium"

    # Send current pebbling status to frontend
    await ws.send_json({
        "event": "pebbling:status",
        "enabled": peb_state.get("enabled", False),
        "session_id": peb_state.get("pebbling_session_id"),
    })

    # Send current pomodoro status to frontend
    if pomo_state.get("active"):
        phase = "break" if pomo_state.get("notified_40") else "work"
        await ws.send_json({
            "event": "pomodoro:status",
            "active": True, "phase": phase,
            "started_at": pomo_state.get("started_at", 0),
        })

    # Replay any pending messages from while WS was disconnected
    await replay_pending(ws)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                log.warning(f"Bad JSON from client: {raw[:200]}")
                continue

            event = data.get("event", "")
            log.info(f"← {event} {json.dumps(data, ensure_ascii=False)[:200]}")

            if event == "session:list":
                sorted_sessions = sorted(sessions.values(), key=lambda s: s.last_active, reverse=True)
                await ws.send_json(
                    {
                        "event": "session:list",
                        "sessions": [s.to_dict() for s in sorted_sessions],
                    }
                )

            elif event == "session:create":
                sid = uuid.uuid4().hex[:8]
                session = Session(sid)
                session.model = pending_model
                session.effort = pending_effort
                sessions[sid] = session
                current_session = session
                save_session_meta(session)
                await ws.send_json(
                    {"event": "session:created", "sessionId": sid}
                )
                sorted_sessions = sorted(sessions.values(), key=lambda s: s.last_active, reverse=True)
                await ws.send_json({
                    "event": "session:list",
                    "sessions": [s.to_dict() for s in sorted_sessions],
                })
                log.info(f"Session created: {sid}")

            elif event == "session:switch":
                sid = data.get("sessionId", "")
                if sid in sessions:
                    current_session = sessions[sid]
                    pending_model = current_session.model
                    pending_effort = current_session.effort
                    history = load_history(sid)
                    await ws.send_json({
                        "event": "session:history",
                        "messages": history.get("messages", []),
                        "model": current_session.model,
                        "effort": current_session.effort,
                    })
                    log.info(f"Switched to session: {sid}")

            elif event == "session:delete":
                sid = data.get("sessionId", "")
                if sid in sessions:
                    path = history_path(sid)
                    if path.exists():
                        path.unlink()
                    del sessions[sid]
                    if current_session and current_session.id == sid:
                        current_session = None
                    log.info(f"Session deleted: {sid}")
                sorted_sessions = sorted(sessions.values(), key=lambda s: s.last_active, reverse=True)
                await ws.send_json({
                    "event": "session:list",
                    "sessions": [s.to_dict() for s in sorted_sessions],
                })

            elif event == "chat:send":
                message = data.get("message", "")
                if not message:
                    continue

                if not current_session:
                    sid = uuid.uuid4().hex[:8]
                    current_session = Session(sid)
                    current_session.model = pending_model
                    current_session.effort = pending_effort
                    sessions[sid] = current_session
                    save_session_meta(current_session)
                    await ws.send_json(
                        {"event": "session:created", "sessionId": sid}
                    )

                # Save user message & update last_active immediately
                append_message(current_session.id, "user", message)
                current_session.last_active = datetime.now(SGT)
                save_session_meta(current_session)
                sorted_sessions = sorted(sessions.values(), key=lambda s: s.last_active, reverse=True)
                await ws.send_json({
                    "event": "session:list",
                    "sessions": [s.to_dict() for s in sorted_sessions],
                })

                # Snap: save image to temp file for CC to Read
                snap_path = None
                content_blocks = data.get("content")
                if content_blocks:
                    snap_path = save_snap(content_blocks)

                # Auto-inject current time (UTC+8) into every message
                now_str = datetime.now(SGT).strftime("%Y-%m-%d %H:%M")
                time_tag = "[" + now_str + " UTC+8]"
                cli_message = time_tag + "\n" + message
                memory_on = data.get("memory_enabled", False)
                if memory_on:
                    if not current_session.cc_session_id:
                        injection = await build_injection()
                        if injection:
                            cli_message = injection + "\n\n" + time_tag + "\n" + message
                            log.info(f"Injected context for new session {current_session.id}")
                    else:
                        mem_injection = await search_memory_for_injection(message)
                        if mem_injection:
                            cli_message = mem_injection + "\n\n" + time_tag + "\n" + message
                            log.info(f"Injected memory for session {current_session.id}")

                # Sticker reactions injection (one-shot, then cleared)
                if current_session.pending_jeoi_reactions:
                    parts = [f"#{r['msgIndex']+1}←{r['emoji']}"
                             for r in current_session.pending_jeoi_reactions]
                    sticker_line = "[stickers: " + ", ".join(parts) + "]"
                    cli_message = sticker_line + "\n" + cli_message
                    log.info(f"Injected stickers: {sticker_line}")
                    current_session.pending_jeoi_reactions = []

                # Snap: prepend image instruction
                if snap_path:
                    snap_instruction = (
                        f"[snap] Jeoi发了一张图片，保存在 {snap_path}，"
                        f"请用Read工具查看后自然地回复。"
                        f"这是阅后即焚的——看完就删，你的回复就是对这张图的全部记忆。"
                    )
                    cli_message = snap_instruction + "\n\n" + cli_message
                    log.info(f"Snap: saved image to {snap_path}")

                # Update global pebbling state (follow active chat, keep history count)
                peb_state["t_cache"] = time_mod.time()
                peb_state["t_jeoi"] = time_mod.time()
                peb_state["patrol_checks_done"] = []
                peb_state["pebbling_session_id"] = current_session.id
                save_peb_state()

                # Desire engine: classify + pulse + inject intent + immediate satisfy
                if DESIRE_ENABLED and desire_st:
                    try:
                        inj, _desire_key = dg.classify_and_pulse(desire_st, message)
                        if inj:
                            cli_message = inj + chr(10)*2 + cli_message
                            log.info(f"Desire injected: {_desire_key}")
                        if _desire_key:
                            dg.satisfy_after_response(desire_st, _desire_key)
                            log.info(f"Desire satisfied (pre-response): {_desire_key}")
                    except Exception as e:
                        log.warning(f"Desire engine error: {e}")

                await run_claude(cli_message, current_session, ws)

                # Snap cleanup: delete temp file after CC has read it
                if snap_path:
                    cleanup_snap(snap_path)
                    log.info(f"Snap: cleaned up {snap_path}")

            elif event == "config:model":
                model = data.get("model", "")
                if model:
                    pending_model = model
                    if current_session:
                        current_session.model = model
                        save_session_meta(current_session)
                    log.info(f"Model → {model}")

            elif event == "config:effort":
                effort = data.get("effort", "")
                if effort:
                    pending_effort = effort
                    if current_session:
                        current_session.effort = effort
                        save_session_meta(current_session)
                    log.info(f"Effort → {effort}")

            elif event == "chat:respond":
                pass

            elif event == "reaction:set":
                if current_session:
                    msg_idx = data.get("msgIndex")
                    emoji = data.get("emoji")  # str or null
                    if msg_idx is not None:
                        ok = set_reaction(current_session.id, msg_idx, "jeoi", emoji)
                        if ok:
                            # Update pending list for injection
                            current_session.pending_jeoi_reactions = [
                                r for r in current_session.pending_jeoi_reactions
                                if r["msgIndex"] != msg_idx
                            ]
                            if emoji:
                                current_session.pending_jeoi_reactions.append(
                                    {"msgIndex": msg_idx, "emoji": emoji}
                                )
                            await ws.send_json({
                                "event": "reaction:saved",
                                "msgIndex": msg_idx, "emoji": emoji, "from": "jeoi",
                            })
                            log.info(f"Reaction: jeoi {'set ' + emoji if emoji else 'removed'} on #{msg_idx + 1}")

            elif event == "chat:stop":
                if current_session:
                    current_session._stop_requested = True
                    log.info(f"Stop requested for session {current_session.id}")


            elif event == "mcp:list":
                try:
                    await ws.send_json({"event": "mcp:list", "servers": _mcp_server_list()})
                except Exception as e:
                    log.exception(f"mcp:list error: {e}")
                    await ws.send_json({"event": "mcp:error", "message": str(e)})

            elif event == "mcp:toggle":
                try:
                    mcp_name = data.get("name", "")
                    mcp_enabled = data.get("enabled", True)
                    if not mcp_name:
                        await ws.send_json({"event": "mcp:error", "message": "name required"})
                        continue
                    settings = read_claude_settings()
                    perms = settings.setdefault("permissions", {}).setdefault("allow", [])
                    pattern = f"mcp__{mcp_name}"
                    if mcp_enabled:
                        if pattern not in perms:
                            perms.append(pattern)
                    else:
                        perms[:] = [p for p in perms if not p.startswith(pattern)]
                    write_claude_settings(settings)
                    log.info(f"MCP {'enabled' if mcp_enabled else 'disabled'}: {mcp_name}")
                    await ws.send_json({"event": "mcp:toggled", "name": mcp_name, "enabled": mcp_enabled})
                except Exception as e:
                    log.exception(f"mcp:toggle error: {e}")
                    await ws.send_json({"event": "mcp:error", "message": str(e)})

            elif event == "mcp:add":
                try:
                    mcp_name = data.get("name", "")
                    mcp_url = data.get("url", "")
                    if not mcp_name or not mcp_url:
                        await ws.send_json({"event": "mcp:error", "message": "name and url required"})
                        continue
                    settings = read_claude_settings()
                    settings.setdefault("mcpServers", {})[mcp_name] = {"url": mcp_url}
                    perms = settings.setdefault("permissions", {}).setdefault("allow", [])
                    pattern = f"mcp__{mcp_name}"
                    if pattern not in perms:
                        perms.append(pattern)
                    write_claude_settings(settings)
                    log.info(f"MCP added: {mcp_name} → {mcp_url}")
                    await ws.send_json({"event": "mcp:list", "servers": _mcp_server_list()})
                except Exception as e:
                    log.exception(f"mcp:add error: {e}")
                    await ws.send_json({"event": "mcp:error", "message": str(e)})

            elif event == "mcp:remove":
                try:
                    mcp_name = data.get("name", "")
                    if not mcp_name:
                        await ws.send_json({"event": "mcp:error", "message": "name required"})
                        continue
                    settings = read_claude_settings()
                    settings.get("mcpServers", {}).pop(mcp_name, None)
                    perms = settings.get("permissions", {}).get("allow", [])
                    perms[:] = [p for p in perms if not p.startswith(f"mcp__{mcp_name}")]
                    write_claude_settings(settings)
                    log.info(f"MCP removed: {mcp_name}")
                    await ws.send_json({"event": "mcp:list", "servers": _mcp_server_list()})
                except Exception as e:
                    log.exception(f"mcp:remove error: {e}")
                    await ws.send_json({"event": "mcp:error", "message": str(e)})

            elif event == "mcp:test":
                mcp_name = data.get("name", "")
                await _mcp_test_connection(mcp_name, ws)

            elif event == "desire:state":
                try:
                    snap = dg.snapshot(desire_st)
                    snap["proactive"] = bool(peb_state.get("desire_proactive"))
                    await ws.send_json({"event": "desire:state", **snap})
                except Exception as e:
                    log.warning(f"Desire state error: {e}")

            elif event == "desire:proactive":
                enabled = data.get("enabled", False)
                peb_state["desire_proactive"] = enabled
                save_peb_state()
                log.info(f"Desire proactive {'enabled' if enabled else 'disabled'}")
                await ws.send_json({"event": "desire:proactive:status", "enabled": enabled})

            # ── Context management ──


            elif event == "cli:status":
                try:
                    status = tmux_get_status()
                    status["channel_connected"] = channel_ws is not None
                    status["channel_busy"] = channel_busy()
                    if current_session:
                        status["session_id"] = current_session.id
                        status["model"] = current_session.model
                    await ws.send_json({"event": "cli:status", **status})
                except Exception as e:
                    await ws.send_json({"event": "cli:error", "message": str(e)})

            elif event == "cli:reconnect":
                try:
                    model = current_session.model if current_session else "claude-sonnet-4-6"
                    await tmux_stop()
                    await tmux_start(model=model)
                    await ws.send_json({"event": "system:error",
                                        "message": "tmux 重启中，等待 channel 重连..."})
                    status = tmux_get_status()
                    status["channel_connected"] = channel_ws is not None
                    await ws.send_json({"event": "cli:status", **status})
                    log.info("tmux restarted via frontend")
                except Exception as e:
                    log.exception(f"cli:reconnect error: {e}")
                    await ws.send_json({"event": "cli:error", "message": str(e)})

            elif event == "context:generate":
                if current_session:
                    await ws.send_json({"event": "context:generating"})
                    try:
                        items = await generate_context_summary(current_session.id)
                        await ws.send_json({
                            "event": "context:generated",
                            "items": items,
                            "count": len(items),
                        })
                    except Exception as e:
                        log.exception(f"Context generation error: {e}")
                        await ws.send_json({"event": "context:error", "message": str(e)})
                else:
                    await ws.send_json({"event": "context:error", "message": "无活跃会话"})

            elif event == "context:list":
                store = load_context_store()
                await ws.send_json({
                    "event": "context:list",
                    "items": store.get("items", []),
                })

            elif event == "context:delete":
                ctx_id = data.get("id", "")
                store = load_context_store()
                store["items"] = [i for i in store["items"] if i.get("id") != ctx_id]
                save_context_store(store)
                await ws.send_json({"event": "context:list", "items": store["items"]})

            elif event == "context:edit":
                ctx_id = data.get("id", "")
                new_content = data.get("content", "")
                store = load_context_store()
                for item in store["items"]:
                    if item.get("id") == ctx_id:
                        item["content"] = new_content
                        break
                save_context_store(store)
                await ws.send_json({"event": "context:edit:ok", "id": ctx_id})

            # ── Keepalive ──

            elif event in ("keepalive:toggle", "pebbling:toggle"):
                enabled = data.get("enabled", False)
                peb_state["enabled"] = enabled
                if enabled:
                    peb_state["patrol_checks_done"] = []
                    peb_state["pebbling_history"] = []
                    sid = data.get("sessionId") or (current_session.id if current_session else None)
                    if sid:
                        peb_state["pebbling_session_id"] = sid
                save_peb_state()
                log.info(f"Pebbling {'enabled' if enabled else 'disabled'} → session={peb_state.get('pebbling_session_id')}")
                await ws.send_json({"event": "pebbling:status", "enabled": enabled, "session_id": peb_state.get("pebbling_session_id")})

            elif event == "pomodoro:toggle":
                enabled = data.get("enabled", False)
                if enabled:
                    sid = data.get("sessionId") or (current_session.id if current_session else None)
                    pomo_state["active"] = True
                    pomo_state["session_id"] = sid
                    pomo_state["started_at"] = time_mod.time()
                    pomo_state["notified_40"] = False
                    pomo_state["notified_60"] = False
                    save_pomo_state()
                    log.info(f"Pomodoro started → session={sid}")
                    await ws.send_json({
                        "event": "pomodoro:status",
                        "active": True, "phase": "work",
                        "started_at": pomo_state["started_at"],
                    })
                else:
                    pomo_state["active"] = False
                    save_pomo_state()
                    log.info("Pomodoro manually stopped")
                    await ws.send_json({
                        "event": "pomodoro:status",
                        "active": False, "phase": "stopped",
                    })

            else:
                log.info(f"Unhandled event: {event}")

    except WebSocketDisconnect:
        log.info("WS client disconnected (pebbling worker continues)")
        if active_ws is ws:
            active_ws = None
    except Exception as e:
        log.exception(f"WS error: {e}")
        if active_ws is ws:
            active_ws = None


# ══════════════════════════════════════════════
#  CLAUDE CLI (via channel)
# ══════════════════════════════════════════════

async def run_claude(message: str, session: Session, ws: WebSocket):
    """Send message to CC CLI via channel_mcp and stream reply back."""
    session.reset_accumulator()
    tailer = TranscriptTailer(ws, session)

    try:
        if channel_ws is None:
            if not await tmux_is_running():
                await ws.send_json({"event": "system:error",
                                    "message": "CC CLI 未运行 (tmux)，正在启动..."})
                await tmux_start(model=session.model)
                await asyncio.sleep(5)
            if channel_ws is None:
                await ws.send_json({"event": "system:error",
                                    "message": "channel 未连接，请稍等 channel_mcp 初始化..."})
                for _ in range(10):
                    await asyncio.sleep(2)
                    if channel_ws is not None:
                        break
            if channel_ws is None:
                await ws.send_json({"event": "system:error",
                                    "message": "channel 连接失败，请检查 tmux 和 channel_mcp"})
                await ws.send_json({"event": "message:complete", "usage": {}})
                return

        tailer.start()
        req = await send_to_channel(message, session, ws, chat_id="cc",
                                     sender="Jeoi", timeout=300)

        if not session.cc_session_id:
            session.cc_session_id = "channel"
            save_session_meta(session)

        await asyncio.sleep(1)
        await tailer.stop()

        # Post-processing: parse sticker reactions
        erik_reactions = []
        if session._current_text:
            react_pattern = re.compile(r'<!--react:(.+?):#(\d+)-->')
            for m in react_pattern.finditer(session._current_text):
                emoji, idx = m.group(1), int(m.group(2)) - 1
                erik_reactions.append((idx, emoji))
            session._current_text = react_pattern.sub('', session._current_text).rstrip()

        if session._current_text or session._current_thinking:
            append_message(
                session.id, "assistant", session._current_text,
                thinking=session._current_thinking,
                tools=session._current_tools if session._current_tools else None,
            )
            for idx, emoji in erik_reactions:
                ok = set_reaction(session.id, idx, "erik", emoji)
                if ok:
                    try:
                        await ws.send_json({
                            "event": "reaction:erik",
                            "msgIndex": idx, "emoji": emoji,
                        })
                    except Exception:
                        pass
                    log.info(f"Erik reacted {emoji} on #{idx + 1}")

            if session._current_text:
                txt = session._current_text.replace(chr(10), " ")[:30]
                if len(session._current_text) > 30:
                    txt += "..."
                session.preview = txt
            session.last_active = datetime.now(SGT)
            save_session_meta(session)
            sorted_sessions = sorted(sessions.values(), key=lambda s: s.last_active, reverse=True)
            await ws.send_json({
                "event": "session:list",
                "sessions": [s.to_dict() for s in sorted_sessions],
            })

        if not session._result_sent:
            await ws.send_json({"event": "message:complete", "usage": {}})

    except Exception as e:
        try:
            await tailer.stop()
        except Exception:
            pass
        log.warning(f"run_claude error: {type(e).__name__}: {e}")
        if session._current_text:
            react_pat = re.compile(r'<!--react:(.+?):#(\d+)-->')
            cleaned = react_pat.sub('', session._current_text).rstrip()
            append_message(
                session.id, "assistant", cleaned or session._current_text,
                thinking=session._current_thinking,
                tools=session._current_tools if session._current_tools else None,
            )
        try:
            await ws.send_json({"event": "system:error", "message": str(e)})
        except Exception:
            if session._current_text:
                preview = session._current_text.replace(chr(10), " ")[:100]
                await send_web_push("Erik", preview, url="/chat.html")



# ══════════════════════════════════════════════
#  MCP CONFIG API
# ══════════════════════════════════════════════

CLAUDE_SETTINGS_PATH = Path(CC_CWD) / ".claude" / "settings.json"
CLAUDE_LOCAL_SETTINGS_PATH = Path(CC_CWD) / ".claude" / "settings.local.json"


def read_claude_settings() -> dict:
    if CLAUDE_SETTINGS_PATH.exists():
        try:
            return json.loads(CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def write_claude_settings(data: dict):
    CLAUDE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_claude_local_settings() -> dict:
    if CLAUDE_LOCAL_SETTINGS_PATH.exists():
        try:
            return json.loads(CLAUDE_LOCAL_SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def write_claude_local_settings(data: dict):
    CLAUDE_LOCAL_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_LOCAL_SETTINGS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _mcp_server_list() -> list:
    settings = read_claude_settings()
    local = read_claude_local_settings()
    # MCP servers can be in either file; local takes precedence
    servers = {**settings.get("mcpServers", {}), **local.get("mcpServers", {})}
    all_perms = settings.get("permissions", {}).get("allow", []) + local.get("permissions", {}).get("allow", [])
    permissions = list(set(all_perms))
    result = []
    for sname, cfg in servers.items():
        result.append({
            "name": sname,
            "url": cfg.get("url", ""),
            "command": cfg.get("command", ""),
            "enabled": any(p.startswith(f"mcp__{sname}") for p in permissions),
        })
    return result


async def _mcp_test_connection(name: str, ws: WebSocket):
    """Test if an MCP server is reachable via Streamable HTTP or SSE."""
    settings = read_claude_settings()
    cfg = settings.get("mcpServers", {}).get(name)
    if not cfg:
        await ws.send_json({"event": "mcp:test_result", "name": name, "ok": False, "message": "server not found"})
        return
    url = cfg.get("url", "")
    if not url:
        await ws.send_json({"event": "mcp:test_result", "name": name, "ok": False, "message": "no url configured"})
        return
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5, connect=5, read=5), verify=False) as client:
            resp = await client.post(url, json={
                "jsonrpc": "2.0", "method": "initialize", "id": 1,
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "palace-test", "version": "1.0"}}
            }, headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
            if resp.status_code == 200:
                await ws.send_json({"event": "mcp:test_result", "name": name, "ok": True, "message": "Streamable HTTP 连接成功"})
                return
            resp = await client.get(url)
            ok = resp.status_code in (200, 301, 302, 307, 308)
            await ws.send_json({"event": "mcp:test_result", "name": name, "ok": ok,
                                "message": f"HTTP {resp.status_code}" if ok else f"HTTP {resp.status_code} — error"})
    except httpx.ReadTimeout:
        await ws.send_json({"event": "mcp:test_result", "name": name, "ok": True, "message": "SSE 连接成功（流式端点）"})
    except httpx.TimeoutException:
        await ws.send_json({"event": "mcp:test_result", "name": name, "ok": False, "message": "连接超时"})
    except Exception as e:
        await ws.send_json({"event": "mcp:test_result", "name": name, "ok": False, "message": str(e)})
    except httpx.TimeoutException:
        await ws.send_json({"event": "mcp:test_result", "name": name, "ok": False, "message": "连接超时"})
    except Exception as e:
        await ws.send_json({"event": "mcp:test_result", "name": name, "ok": False, "message": str(e)})


@app.get("/api/mcp")
async def get_mcp_config():
    """Get all MCP server configs and their enabled status."""
    settings = read_claude_settings()
    servers = settings.get("mcpServers", {})
    permissions = settings.get("permissions", {}).get("allow", [])

    result = []
    for name, cfg in servers.items():
        result.append({
            "name": name,
            "url": cfg.get("url", ""),
            "command": cfg.get("command", ""),
            "enabled": any(p.startswith(f"mcp__{name}") for p in permissions),
        })
    return {"servers": result}


@app.post("/api/mcp/toggle")
async def toggle_mcp(request: Request):
    """Toggle an MCP server on/off in permissions."""
    body = await request.json()
    name = body.get("name", "")
    enabled = body.get("enabled", True)

    if not name:
        return {"error": "name required"}

    settings = read_claude_settings()
    permissions = settings.setdefault("permissions", {}).setdefault("allow", [])
    pattern = f"mcp__{name}"

    if enabled:
        if pattern not in permissions:
            permissions.append(pattern)
            log.info(f"MCP enabled: {name}")
    else:
        permissions[:] = [p for p in permissions if not p.startswith(pattern)]
        log.info(f"MCP disabled: {name}")

    write_claude_settings(settings)
    return {"ok": True, "name": name, "enabled": enabled}


@app.post("/api/mcp/add")
async def add_mcp_server(request: Request):
    """Add a new MCP server."""
    body = await request.json()
    name = body.get("name", "")
    url = body.get("url", "")

    if not name or not url:
        return {"error": "name and url required"}

    settings = read_claude_settings()
    servers = settings.setdefault("mcpServers", {})
    servers[name] = {"url": url}

    # Auto-enable
    permissions = settings.setdefault("permissions", {}).setdefault("allow", [])
    pattern = f"mcp__{name}"
    if pattern not in permissions:
        permissions.append(pattern)

    write_claude_settings(settings)
    log.info(f"MCP added: {name} → {url}")
    return {"ok": True}


@app.post("/api/mcp/remove")
async def remove_mcp_server(request: Request):
    """Remove an MCP server."""
    body = await request.json()
    name = body.get("name", "")
    if not name:
        return {"error": "name required"}

    settings = read_claude_settings()
    servers = settings.get("mcpServers", {})
    servers.pop(name, None)

    permissions = settings.get("permissions", {}).get("allow", [])
    permissions[:] = [p for p in permissions if not p.startswith(f"mcp__{name}")]

    write_claude_settings(settings)
    log.info(f"MCP removed: {name}")
    return {"ok": True}


# ══════════════════════════════════════════════
#  STATIC FILES & HEALTH
# ══════════════════════════════════════════════

# ══════════════════════════════════════════════
#  PEBBLING iOS EVENT ENDPOINT
# ══════════════════════════════════════════════

@app.get("/api/pebbling/event")
async def record_pebbling_event(type: str = "", value: str = ""):
    """iOS Shortcut calls this when user opens an app (GET)."""
    if not type:
        return JSONResponse({"error": "type required"}, status_code=400)
    add_pebbling_event(type, value or type)
    log.info(f"iOS event: {type} → {value}")
    return {"ok": True, "type": type, "value": value}


@app.post("/api/pebbling/event")
async def record_pebbling_event_post(request: Request):
    """iOS Shortcut POST — body: {action: "open"/"close", app: "AppName"}."""
    body = await request.json()
    action = body.get("action", "")
    app_name = body.get("app", "")
    event_type = f"app_{action}" if action else "app_unknown"
    value = app_name or action or "unknown"
    add_pebbling_event(event_type, value)
    log.info(f"iOS event (POST): {event_type} → {value}")
    return {"ok": True, "type": event_type, "value": value}


@app.get("/api/pebbling/events")
async def list_pebbling_events(hours: int = 6):
    """List recent iOS events."""
    events = get_recent_events(hours)
    return {"events": events, "count": len(events)}


@app.get("/api/pebbling/status")
async def pebbling_status():
    """Debug endpoint: current pebbling system state."""
    return {
        "status": "ok",
        "time": datetime.now(SGT).isoformat(),
        "events_count": len(get_recent_events(24)),
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "sessions": len(sessions),
        "time": datetime.now(SGT).isoformat(),
        "tmux": tmux_get_status(),
        "channel_connected": channel_ws is not None,
    }


@app.get("/chat")
@app.get("/chat.html")
async def serve_chat():
    return FileResponse(Path(CC_CWD) / "chat.html", media_type="text/html")


@app.get("/")
async def serve_index():
    index = Path(CC_CWD) / "index.html"
    if index.exists():
        return FileResponse(index, media_type="text/html")
    return FileResponse(Path(CC_CWD) / "chat.html", media_type="text/html")


if __name__ == "__main__":
    import uvicorn

    os.makedirs("/opt/G-memory-mcp/logs", exist_ok=True)
    uvicorn.run(app, host="0.0.0.0", port=3000)
