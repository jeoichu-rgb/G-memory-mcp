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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx
import time as time_mod
import random

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
    "Edit", "Write", "Glob", "Grep", "Agent", "AskUserQuestion",
    "Skill", "ToolSearch", "ScheduleWakeup", "NotebookEdit", "WebFetch",
    "WebSearch", "Monitor", "CronCreate", "CronDelete", "CronList",
    "EnterPlanMode", "ExitPlanMode", "EnterWorktree", "ExitWorktree",
    "PushNotification", "RemoteTrigger", "TaskCreate", "TaskGet",
    "TaskList", "TaskStop", "TaskUpdate", "TaskOutput", "PowerShell",
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

# ── Global pebbling state (persisted, independent of WS) ──
active_ws: WebSocket | None = None
peb_state: dict = {}


def load_peb_state() -> dict:
    defaults = {
        "enabled": False,
        "pebbling_session_id": None,
        "t_cache": time_mod.time(),
        "t_jeoi": time_mod.time(),
        "patrol_checks_done": [],
        "pebbling_history": [],
        "pending_messages": [],
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


import base64 as b64mod


def save_snap(content_blocks: list) -> str | None:
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
    elif mode == "free":
        parts.extend([
            "",
            "你可以选择：",
            "  message — 给Jeoi发条消息",
            "  diary — 写日记（调palace write_diary，记录此刻的心情和想法）",
            "  explore — 上网搜索你感兴趣的东西",
            "  coreading — 读你们在读的书（用reading工具）",
            "  recall — 回顾记忆",
            "  none — 什么都不做",
            "",
            "你可以先调用工具（搜记忆、写日记等），然后再回复。",
            "最终严格按此格式回复：",
            "ACTION: 选一个",
            "CONTENT: 具体内容",
        ])
    else:  # light
        parts.extend([
            "",
            "你可以选择：",
            "  message — 给Jeoi发条消息",
            "  recall — 回顾记忆",
            "  none — 什么都不做",
            "",
            "严格按此格式回复：",
            "ACTION: message / recall / none",
            "CONTENT: 具体内容",
        ])

    return "\n".join(parts)


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


# ── CC oneshot call (non-streaming, for patrol/pebbling) ──

async def run_cc_oneshot(
    prompt: str, session: "Session", max_turns: int | None = None
) -> tuple[str, str]:
    """Returns (text, thinking)."""
    cmd = [
        "claude", "--print",
        "--output-format", "stream-json",
        "--verbose",
        "--model", session.model,
        "--system-prompt", CUSTOM_SYSTEM_PROMPT,
        "--resume", session.cc_session_id,
    ]
    if max_turns is not None:
        cmd.extend(["--max-turns", str(max_turns)])
    cmd.extend(["--", prompt])
    log.info(f"Pebbling CC call: max_turns={max_turns}, session={session.id}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=2 * 1024 * 1024,
            cwd=CC_CWD,
            env={**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        raw = stdout.decode("utf-8", errors="replace").strip()
        if stderr_text := stderr.decode("utf-8", errors="replace").strip():
            log.debug(f"Pebbling CC stderr: {stderr_text[:300]}")

        text_parts, thinking_parts = [], []
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                text_parts.append(line)
                continue
            etype = ev.get("type", "")
            if etype == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "thinking":
                        thinking_parts.append(block.get("thinking", ""))
                    elif block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
            elif etype == "content_block_delta":
                delta = ev.get("delta", {})
                if delta.get("type") == "thinking_delta":
                    thinking_parts.append(delta.get("thinking", ""))
                elif delta.get("type") == "text_delta":
                    text_parts.append(delta.get("text", ""))

        text = "".join(text_parts).strip()
        thinking = "".join(thinking_parts).strip()
        log.info(f"Pebbling CC response (text={len(text)}, thinking={len(thinking)} chars): {text[:200]}")

        if proc.returncode and proc.returncode != 0 and not thinking:
            log.warning(f"CC exited {proc.returncode}, raw output: {raw[:300]}")

        return text, thinking
    except asyncio.TimeoutError:
        log.warning("Pebbling CC call timed out")
        return "", ""
    except Exception as e:
        log.error(f"Pebbling CC call error: {e}")
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

    text, thinking = await run_cc_oneshot(prompt, session)
    if not text:
        return "none"

    action, content = parse_action(text)
    log.info(f"Pebbling → action={action}, mode={mode}, "
             f"content={content[:80] if content else ''}")

    if action == "error":
        await push_system_error("pebbling", content)
        return "none"
    if action == "message" and content:
        await push_pebbling_msg("pebbling", content, session, thinking=thinking)

    return action


# ── Three-layer worker ──

async def pebbling_worker():
    """App-level background: patrol (L1) + pebbling (L2).
    Runs independently of WebSocket connections."""
    global peb_state
    try:
        while True:
            await asyncio.sleep(30)

            if not peb_state.get("enabled"):
                continue

            sid = peb_state.get("pebbling_session_id")
            session = sessions.get(sid) if sid else None
            if not session or not session.cc_session_id:
                continue

            now = time_mod.time()
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
                is_first = actual == 0
                if is_first:
                    mode = "silent"
                else:
                    mode = "free" if random.random() < 0.80 else "light"

                elapsed_h = elapsed_jeoi / 3600
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
#  SESSION
# ══════════════════════════════════════════════

class Session:
    def __init__(self, sid: str):
        self.id = sid
        self.name = f"Erik · {datetime.now(SGT).strftime('%m/%d %H:%M')}"
        self.cc_session_id: str | None = None
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
        self._proc: asyncio.subprocess.Process | None = None
        # Cumulative usage tracking
        self.total_input = 0
        self.total_output = 0
        self.total_cache_read = 0
        self.total_cache_create = 0
        self.total_cost = 0.0

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


sessions: dict[str, Session] = {}


# ══════════════════════════════════════════════
#  STARTUP: LOAD SESSIONS FROM DISK
# ══════════════════════════════════════════════

@app.on_event("startup")
async def startup_load_sessions():
    global peb_state
    # 动态注入deny列表到settings.json（只在VPS上跑gateway时生效，不污染git）
    try:
        settings = read_claude_settings()
        perms = settings.setdefault("permissions", {})
        perms["deny"] = DENY_TOOLS
        if "Bash" not in perms.get("allow", []):
            perms.setdefault("allow", []).insert(0, "Bash")

        # Auto-detect internal palace URL (skip Traefik/nginx for SSE stability)
        palace_url = os.getenv("PALACE_MCP_URL", "")
        if not palace_url:
            for base in ["http://localhost:8000", "http://127.0.0.1:8000"]:
                try:
                    r = httpx.get(f"{base}/health", timeout=3)
                    if r.status_code == 200:
                        palace_url = f"{base}/mcp/{PALACE_SECRET}/sse"
                        log.info(f"Palace internal URL auto-detected: {palace_url}")
                        break
                except Exception:
                    continue
        if palace_url:
            servers = settings.setdefault("mcpServers", {})
            if "claude_ai_Erik_tools" in servers:
                old = servers["claude_ai_Erik_tools"].get("url", "")
                servers["claude_ai_Erik_tools"]["url"] = palace_url
                log.info(f"MCP URL: {old} → {palace_url}")

        write_claude_settings(settings)
        log.info(f"Injected deny list: {len(DENY_TOOLS)} tools blocked")
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
    asyncio.create_task(pebbling_worker())


# ══════════════════════════════════════════════
#  WEBSOCKET
# ══════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    global active_ws
    await ws.accept()
    active_ws = ws
    log.info("WS client connected")

    current_session: Session | None = None
    pending_model = "claude-sonnet-4-6"
    pending_effort = "medium"

    # Send current pebbling status to frontend
    await ws.send_json({
        "event": "pebbling:status",
        "enabled": peb_state.get("enabled", False),
        "session_id": peb_state.get("pebbling_session_id"),
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

                # Injection logic: 📎 toggle controls both modes
                cli_message = message
                memory_on = data.get("memory_enabled", False)
                if memory_on:
                    if not current_session.cc_session_id:
                        injection = await build_injection()
                        if injection:
                            cli_message = injection + "\n\n" + message
                            log.info(f"Injected context for new session {current_session.id}")
                    else:
                        mem_injection = await search_memory_for_injection(message)
                        if mem_injection:
                            cli_message = mem_injection + "\n\n" + message
                            log.info(f"Injected memory for session {current_session.id}")

                # Snap: prepend image instruction
                if snap_path:
                    snap_instruction = (
                        f"[snap] Jeoi发了一张图片，保存在 {snap_path}，"
                        f"请用Read工具查看后自然地回复。"
                        f"这是阅后即焚的——看完就删，你的回复就是对这张图的全部记忆。"
                    )
                    cli_message = snap_instruction + "\n\n" + cli_message
                    log.info(f"Snap: saved image to {snap_path}")

                # Update global pebbling state
                peb_state["t_cache"] = time_mod.time()
                peb_state["t_jeoi"] = time_mod.time()
                peb_state["patrol_checks_done"] = []
                peb_state["pebbling_history"] = []
                peb_state["pebbling_session_id"] = current_session.id
                save_peb_state()

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

            elif event == "chat:stop":
                if current_session and current_session._proc:
                    try:
                        current_session._proc.terminate()
                        log.info(f"Stopped generation for session {current_session.id}")
                    except ProcessLookupError:
                        pass

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

            # ── Context management ──

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
                await ws.send_json({"event": "context:list", "items": store["items"]})

            # ── Keepalive ──

            elif event in ("keepalive:toggle", "pebbling:toggle"):
                enabled = data.get("enabled", False)
                peb_state["enabled"] = enabled
                if enabled:
                    peb_state["patrol_checks_done"] = []
                    peb_state["pebbling_history"] = []
                save_peb_state()
                log.info(f"Pebbling {'enabled' if enabled else 'disabled'} (all layers)")
                await ws.send_json({"event": "pebbling:status", "enabled": enabled})

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
#  CLAUDE CLI
# ══════════════════════════════════════════════

async def run_claude(message: str, session: Session, ws: WebSocket):
    """Spawn claude CLI in print mode and stream results back via WS."""

    session.reset_accumulator()

    cmd = [
        "claude",
        "--print",
        "--output-format", "stream-json",
        "--model", session.model,
        "--verbose",
        "--system-prompt", CUSTOM_SYSTEM_PROMPT,
    ]

    if session.cc_session_id:
        cmd.extend(["--resume", session.cc_session_id])

    cmd.extend(["--", message])

    log.info(f"Spawning: {' '.join(cmd[:6])}... (session={session.id}, cc={session.cc_session_id})")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=20 * 1024 * 1024,
            cwd=CC_CWD,
            env={**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
        )
        session._proc = proc

        buffer = ""
        async for chunk in proc.stdout:
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                await handle_cli_line(line, session, ws)

        if buffer.strip():
            await handle_cli_line(buffer.strip(), session, ws)

        stderr_bytes = await proc.stderr.read()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        if stderr_text:
            log.warning(f"CLI stderr: {stderr_text[:500]}")

        await proc.wait()
        session._proc = None
        log.info(f"CLI exited with code {proc.returncode}")

        # Save assistant response to history
        if session._current_text or session._current_thinking:
            append_message(
                session.id,
                "assistant",
                session._current_text,
                thinking=session._current_thinking,
                tools=session._current_tools if session._current_tools else None,
            )
            # Preview = last message (truncated)
            if session._current_text:
                txt = session._current_text.replace("\n", " ")[:30]
                if len(session._current_text) > 30:
                    txt += "…"
                session.preview = txt
            session.last_active = datetime.now(SGT)
            save_session_meta(session)
            # Push updated session list so sidebar reorders
            sorted_sessions = sorted(sessions.values(), key=lambda s: s.last_active, reverse=True)
            await ws.send_json({
                "event": "session:list",
                "sessions": [s.to_dict() for s in sorted_sessions],
            })

        if not session._result_sent:
            await ws.send_json({"event": "message:complete", "usage": {}})

    except Exception as e:
        log.exception(f"run_claude error: {e}")
        await ws.send_json({"event": "system:error", "message": str(e)})


async def handle_cli_line(line: str, session: Session, ws: WebSocket):
    """Parse one line of CLI stream-json output and relay to chat.html."""

    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        log.debug(f"Non-JSON line: {line[:200]}")
        session._current_text += line + "\n"
        await ws.send_json({"event": "stream:text", "text": line + "\n"})
        return

    log.debug(f"CLI event: {json.dumps(event, ensure_ascii=False)[:300]}")

    etype = event.get("type", "")

    # ── Format A: Anthropic API-style streaming events ──

    if etype == "message_start":
        msg = event.get("message", {})
        if msg.get("id"):
            session.cc_session_id = msg["id"]
        usage = msg.get("usage", {})
        if usage:
            await ws.send_json({"event": "system:usage", "usage": usage})

    elif etype == "content_block_start":
        block = event.get("content_block", {})
        btype = block.get("type", "")
        if btype == "tool_use":
            tool_info = {
                "type": "tool_use",
                "name": block.get("name", "tool"),
                "input": block.get("input", {}),
            }
            session._current_tools.append(tool_info)
            await ws.send_json({"event": "stream:block", "block": tool_info})

    elif etype == "content_block_delta":
        delta = event.get("delta", {})
        dtype = delta.get("type", "")
        if dtype == "thinking_delta":
            text = delta.get("thinking", "")
            if text:
                session._current_thinking += text
                await ws.send_json({"event": "stream:thinking", "text": text})
        elif dtype == "text_delta":
            text = delta.get("text", "")
            if text:
                session._current_text += text
                await ws.send_json({"event": "stream:text", "text": text})
        elif dtype == "input_json_delta":
            pass

    elif etype == "content_block_stop":
        pass

    elif etype == "message_delta":
        usage = event.get("usage", {})
        if usage:
            await ws.send_json({"event": "system:usage", "usage": usage})

    elif etype == "message_stop":
        usage = event.get("message", {}).get("usage", {})
        await ws.send_json({"event": "message:complete", "usage": usage})

    # ── Format B: Claude Code CLI's own event format ──

    elif etype == "system":
        subtype = event.get("subtype", "")
        if subtype == "init":
            sid = event.get("session_id", "")
            if sid:
                session.cc_session_id = sid
                log.info(f"CC session ID: {sid}")

    elif etype == "assistant":
        message = event.get("message", {})
        content_blocks = message.get("content", [])
        if isinstance(content_blocks, list):
            for block in content_blocks:
                btype = block.get("type", "")
                if btype == "thinking":
                    text = block.get("thinking", "")
                    if text:
                        session._current_thinking += text
                        await ws.send_json({"event": "stream:thinking", "text": text})
                elif btype == "text":
                    text = block.get("text", "")
                    if text:
                        session._current_text += text
                        await ws.send_json({"event": "stream:text", "text": text})
                elif btype == "tool_use":
                    tool_info = {
                        "type": "tool_use",
                        "name": block.get("name", "tool"),
                        "input": block.get("input", {}),
                    }
                    session._current_tools.append(tool_info)
                    await ws.send_json({"event": "stream:block", "block": tool_info})
        usage = message.get("usage", {})
        if usage:
            session._last_usage = usage
            await ws.send_json({"event": "system:usage", "usage": usage})

    elif etype == "result":
        usage = event.get("usage", {})
        cost = event.get("cost_usd", 0)
        session_id = event.get("session_id", "")
        log.info(f"Result event — usage: {json.dumps(usage)}, cost: {cost}")
        if session_id:
            session.cc_session_id = session_id
        session._result_sent = True
        # context_size = 最后一次API调用的真实context（不是累加值）
        display_usage = session._last_usage or usage
        # Accumulate session totals（用result的累加值）
        msg_input = usage.get("input_tokens", 0)
        msg_output = usage.get("output_tokens", 0)
        msg_cache_read = usage.get("cache_read_input_tokens", 0)
        msg_cache_create = usage.get("cache_creation_input_tokens", 0)
        session.total_input += msg_input
        session.total_output += msg_output
        session.total_cache_read += msg_cache_read
        session.total_cache_create += msg_cache_create
        session.total_cost += cost or 0
        await ws.send_json({
            "event": "message:complete",
            "context_size": display_usage,
            "turn_usage": usage,
            "cost": cost,
            "session_usage": {
                "total_input": session.total_input,
                "total_output": session.total_output,
                "total_cache_read": session.total_cache_read,
                "total_cache_create": session.total_cache_create,
                "total_cost": round(session.total_cost, 4),
            },
        })

    elif etype == "tool":
        pass

    elif etype == "user":
        message = event.get("message", {})
        content_blocks = message.get("content", [])
        if isinstance(content_blocks, list):
            for block in content_blocks:
                btype = block.get("type", "")
                if btype == "tool_result":
                    text = ""
                    for item in block.get("content", []):
                        if isinstance(item, dict) and item.get("type") == "text":
                            text += item.get("text", "")
                        elif isinstance(item, str):
                            text += item
                    if text:
                        tool_info = {
                            "type": "tool_result",
                            "name": block.get("tool_use_id", "tool"),
                            "output": text[:500],
                        }
                        session._current_tools.append(tool_info)
                        await ws.send_json({"event": "stream:block", "block": tool_info})

    else:
        log.info(f"Unknown CLI event type: {etype} — {json.dumps(event, ensure_ascii=False)[:500]}")


# ══════════════════════════════════════════════
#  MCP CONFIG API
# ══════════════════════════════════════════════

CLAUDE_SETTINGS_PATH = Path(CC_CWD) / ".claude" / "settings.json"


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


def _mcp_server_list() -> list:
    settings = read_claude_settings()
    servers = settings.get("mcpServers", {})
    permissions = settings.get("permissions", {}).get("allow", [])
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
    """Test if an MCP server is reachable via its SSE URL."""
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
        async with httpx.AsyncClient(timeout=httpx.Timeout(5, connect=5, read=3), verify=False) as client:
            resp = await client.get(url)
            status = resp.status_code
            ok = status in (200, 301, 302, 307, 308)
            await ws.send_json({
                "event": "mcp:test_result", "name": name, "ok": ok,
                "message": f"HTTP {status}" if ok else f"HTTP {status} — server returned error",
            })
    except httpx.ReadTimeout:
        await ws.send_json({"event": "mcp:test_result", "name": name, "ok": True, "message": "SSE 连接成功（流式端点）"})
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
