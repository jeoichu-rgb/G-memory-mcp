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
    "Read", "Edit", "Write", "Glob", "Grep", "Agent", "AskUserQuestion",
    "Skill", "ToolSearch", "ScheduleWakeup", "NotebookEdit", "WebFetch",
    "WebSearch", "Monitor", "CronCreate", "CronDelete", "CronList",
    "EnterPlanMode", "ExitPlanMode", "EnterWorktree", "ExitWorktree",
    "PushNotification", "RemoteTrigger", "TaskCreate", "TaskGet",
    "TaskList", "TaskStop", "TaskUpdate", "TaskOutput", "PowerShell",
]


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
                    pass
            loaded.append(session)
        except Exception as e:
            log.warning(f"Failed to load session {f}: {e}")
    return loaded


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
        # Cumulative usage tracking
        self.total_input = 0
        self.total_output = 0
        self.total_cache_read = 0
        self.total_cache_create = 0
        self.total_cost = 0.0

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "preview": self.preview,
            "time": self.created_at.strftime("%H:%M"),
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
    # 动态注入deny列表到settings.json（只在VPS上跑gateway时生效，不污染git）
    try:
        settings = read_claude_settings()
        perms = settings.setdefault("permissions", {})
        perms["deny"] = DENY_TOOLS
        if "Bash" not in perms.get("allow", []):
            perms.setdefault("allow", []).insert(0, "Bash")
        write_claude_settings(settings)
        log.info(f"Injected deny list: {len(DENY_TOOLS)} tools blocked")
    except Exception as e:
        log.warning(f"Failed to inject deny list: {e}")

    loaded = load_all_sessions()
    for s in loaded:
        sessions[s.id] = s
    log.info(f"Loaded {len(loaded)} sessions from disk")


# ══════════════════════════════════════════════
#  WEBSOCKET
# ══════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    log.info("WS client connected")

    current_session: Session | None = None
    pending_model = "claude-sonnet-4-6"
    pending_effort = "medium"

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
                log.info(f"Session created: {sid}")

            elif event == "session:switch":
                sid = data.get("sessionId", "")
                if sid in sessions:
                    current_session = sessions[sid]
                    # Send chat history to client
                    history = load_history(sid)
                    await ws.send_json(
                        {"event": "session:history", "messages": history.get("messages", [])}
                    )
                    log.info(f"Switched to session: {sid}")

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

                # Save user message
                append_message(current_session.id, "user", message)

                await run_claude(message, current_session, ws)

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

            else:
                log.info(f"Unhandled event: {event}")

    except WebSocketDisconnect:
        log.info("WS client disconnected")
    except Exception as e:
        log.exception(f"WS error: {e}")


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
            # Preview = Erik's reply (truncated), sorted by last_active
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
