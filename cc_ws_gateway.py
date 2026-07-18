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
import getpass
from datetime import datetime, timezone, timedelta
from pathlib import Path

_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
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

# 前端可能从主域打开（管理面板入口），API 却走 chat 子域绝对地址——放行跨域。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://erikssheep.uk", "https://chat.erikssheep.uk"],
    allow_methods=["*"],
    allow_headers=["*"],
)

PALACE_SECRET = os.getenv("PALACE_SECRET", "")
CC_CWD = os.getenv("CC_CWD", "/opt/G-memory-mcp")
TG_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
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
GSVI_BASE_URL = os.getenv("GSVI_BASE_URL", "https://gsvi.erikssheep.uk")
CONTEXT_STORE_PATH = Path(CC_CWD) / "context_store.json"
SNAP_DIR = Path("/tmp/snap")
SNAP_DIR.mkdir(exist_ok=True)

# ── Pebbling constants ──
PATROL_SCHEDULE = [5, 10, 20]  # minutes after Jeoi's last msg → always call CC
PEBBLING_INTERVAL = 3 * 3600  # 3 hours
PEBBLING_MAX_24H = 8
EVENTS_PATH = Path(CC_CWD) / "pebbling_events.json"
PEBBLING_STATE_PATH = Path(CC_CWD) / "pebbling_state.json"
LAST_MODEL_PATH = Path(CC_CWD) / "last_model.txt"

# ── Stardew event push ──
# 星露谷 MCP server（Windows :7845）检测到新游戏事件（talk/gift）时
# POST 到 /api/stardew/event，由 stardew_event_worker 防抖合并后
# 注入到 pebbling_session_id 绑定的那一个 session（与 desire/pebbling 同一绑定，
# 自动跟随 Jeoi 最后说话的 session——绝不广播到其他 session）。
_stardew_pending: list = []
_stardew_last_event_ts: float = 0.0
STARDEW_EVENT_DEBOUNCE = 4    # 秒：等事件停止到达再注入（合并连续点击）
STARDEW_EVENT_MAX_AGE = 600   # 秒：积压超过这个年龄的事件直接丢弃


def _load_last_model() -> str:
    try:
        m = LAST_MODEL_PATH.read_text(encoding="utf-8").strip()
        return m if m else "claude-sonnet-4-6"
    except Exception:
        return "claude-sonnet-4-6"


def _save_last_model(model: str):
    try:
        LAST_MODEL_PATH.write_text(model, encoding="utf-8")
    except Exception:
        pass

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
        "id": "reddit",
        "label": "逛Reddit",
        "desc": "用Reddit工具刷刷感兴趣的subreddit——看看热帖、搜个话题、围观评论区吵架；一定要给看过的帖子点赞/点踩，别忘了在有感触的帖子里留下评论",
        "action": "explore",
    },
    {
        "id": "browser",
        "label": "上网冲浪",
        "desc": "用browser工具去Yahoo搜点感兴趣的东西（browser_js提取JSON最省token），看看新闻、查个话题、随便逛逛",
        "action": "explore",
    },
    {
        "id": "recall",
        "label": "记忆漫游",
        "desc": "在记忆库里搜索一个你突然想到的关键词，看看能捞到什么",
        "action": "recall",
    },
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
_ws_last_activity = 0.0  # last time we received anything from the frontend WS
_WS_STALE_SECS = 120  # if no client message for this long, treat WS as potentially stale
peb_state: dict = {}
pomo_state: dict = {}
desire_st = None
_desire_last_tick = 0.0
_desire_last_proactive: dict[str, float] = {}
_user_msg_active = False  # True while run_claude is processing a user message
_recent_libido_memories: list[str] = []

# ── Channel + tmux ──
TMUX_SESSION = os.getenv("CC_TMUX_SESSION", "cc_cli")
CC_USER = os.getenv("CC_USER", "erik")
# Watchdog: mid-turn, if the transcript stops growing for this long, CC is
# almost certainly wedged on a dead MCP call (they have no client timeout —
# a half-open SSE connection blocks forever). Send Esc to cancel the call so
# queued messages can flow again. 0 disables. Must be shorter than the
# wait_done timeout (300s) or the rescue never fires.
STALL_ESC_SECS = int(os.getenv("CC_STALL_ESC_SECS", "240"))
_SU_PFX = "" if getpass.getuser() == CC_USER else f"sudo -u {CC_USER} "
# CC session id the tmux CC CLI is actually on. Set by tmux_start (resume_id or
# None for a fresh session), cleared by tmux_stop, synced after each turn.
# Never guess this from transcript mtimes — forge/scripts writing new JSONL
# files made the freshest file look like the running session, so the gateway
# skipped --resume and fed messages to a stale CC instance.
# Persisted to disk: tmux outlives gateway restarts, so the last known id
# must too — otherwise every gateway restart would force a CC restart.
_TMUX_CC_ID_FILE = Path(CC_CWD) / ".tmux_cc_id"


def _set_tmux_cc_id(val):
    global _tmux_cc_id
    _tmux_cc_id = val
    try:
        if val:
            _TMUX_CC_ID_FILE.write_text(val, encoding="utf-8")
        elif _TMUX_CC_ID_FILE.exists():
            _TMUX_CC_ID_FILE.unlink()
    except OSError as e:
        log.warning(f"tmux_cc_id persist failed: {e}")


def _load_tmux_cc_id():
    try:
        if _TMUX_CC_ID_FILE.exists():
            return _TMUX_CC_ID_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    return None


_tmux_cc_id = _load_tmux_cc_id()

# Last-good session (forge guide §8/§10): only a VERIFIED session gets marked.
# A freshly forged JSONL is never trusted until the verifier saw CC reply in it.
# Consumers: manual recovery, watchdog — never resume anything newer than this
# without verification.
_LAST_GOOD_FILE = Path(CC_CWD) / ".last_good_session"


def save_last_good(frontend_sid: str, cc_id: str):
    try:
        _LAST_GOOD_FILE.write_text(json.dumps({
            "frontend_sid": frontend_sid,
            "cc_session_id": cc_id,
            "verified_at": datetime.now(SGT).isoformat(),
        }, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning(f"last_good persist failed: {e}")


def load_last_good() -> dict | None:
    try:
        if _LAST_GOOD_FILE.exists():
            return json.loads(_LAST_GOOD_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return None


_tmux_send_lock = asyncio.Lock()
channel_ws = None  # legacy
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
    candidates = [p for p in CC_TRANSCRIPT_DIR.glob("*.jsonl")
                  if ".pre-forge-" not in p.name]
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
        self._reply_done = asyncio.Event()
        self._dbg = 0
        self._start_offset = 0
        self._tts_queue: asyncio.Queue | None = None
        self._tts_worker: asyncio.Task | None = None
        # Events stamped before this moment are history, not this turn's reply.
        # When CC CLI resumes it forks old events into a NEW transcript; the
        # tailer switches to that file at offset 0 and would otherwise replay
        # them — spamming the frontend with stale text and hitting a stale
        # stop_reason=end_turn that ends the turn before the real reply lands.
        self._started_at = datetime.now(timezone.utc)
        self._last_growth = time_mod.monotonic()
        # Anchor: don't treat anything as this turn's reply until our own
        # user message shows up in the transcript. A background oneshot
        # (desire/pebbling) already in flight when Jeoi sends a message
        # otherwise lands its ACTION/CONTENT reply in our window — the
        # frontend shows it as HER reply and its end_turn kills the turn
        # before the real reply is even generated.
        self._anchor = None
        self._anchored = True
        self._anchor_armed_at = 0.0

    def start(self, anchor_text: str = None):
        global _transcript_path_cache
        _transcript_path_cache = None
        self._reply_done.clear()
        if anchor_text:
            self._anchor = anchor_text.strip()[:60]
            self._anchored = False
            self._anchor_armed_at = time_mod.monotonic()
        self._path = _find_active_transcript()
        if self._path:
            try:
                self._offset = self._path.stat().st_size
            except Exception:
                self._offset = 0
            self._start_offset = self._offset
            self._task = asyncio.create_task(self._run())
            log.info(f"tailer started: {self._path.name} @{self._offset}")
        if self.session._in_call:
            self._tts_queue = asyncio.Queue()
            self._tts_worker = asyncio.create_task(self._tts_worker_loop())

    async def wait_done(self, timeout=300):
        deadline = time_mod.monotonic() + timeout
        esc_sent = False
        while True:
            remaining = deadline - time_mod.monotonic()
            if remaining <= 0:
                log.warning(f"tailer wait_done timeout ({timeout}s)")
                return
            try:
                await asyncio.wait_for(self._reply_done.wait(),
                                       timeout=min(remaining, 1))
                return
            except asyncio.TimeoutError:
                pass
            if self.session._stop_requested:
                # Jeoi hit stop: /api/chat-stop already sent Esc to CC.
                # Give the tailer one beat to pick up anything CC flushed
                # before the interrupt, then end the turn — run_claude's
                # normal wrap-up (partial text, usage, message:complete)
                # takes it from here.
                log.info("stop requested — ending turn early")
                await asyncio.sleep(1)
                return
            stalled = time_mod.monotonic() - self._last_growth
            if STALL_ESC_SECS and stalled >= STALL_ESC_SECS and not esc_sent:
                log.warning(
                    f"transcript stalled {int(stalled)}s mid-turn "
                    f"(likely a hung MCP call) — sending Esc to rescue CC")
                await tmux_send_escape()
                esc_sent = True

    async def stop(self):
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if self._task and not self._task.done():
                    self._task.cancel()
        if self._tts_worker and not self._tts_worker.done():
            self._tts_worker.cancel()

    async def _run(self):
        _ticks = 0
        while not self._stop.is_set():
            _ticks += 1
            # Anchor fallback: if our message never shows up (text rewritten
            # in transit?), degrade to unanchored tailing rather than eating
            # the whole reply. 20s is enough for a stuck oneshot to finish.
            if (not self._anchored
                    and time_mod.monotonic() - self._anchor_armed_at > 20):
                self._anchored = True
                log.warning("tailer anchor timeout — falling back to unanchored")
            try:
                # Every ~4s, rescan for newer transcript file
                if _ticks % 10 == 0:
                    global _transcript_path_cache
                    _transcript_path_cache = None
                    latest = _find_active_transcript()
                    if latest and latest != self._path:
                        log.info(f"tailer switch: {self._path.name if self._path else 'none'} -> {latest.name}")
                        self._path = latest
                        self._offset = 0
                        # New file, old byte offset is meaningless. Usage
                        # accounting is guarded by _started_at instead.
                        self._start_offset = 0
                        self._last_growth = time_mod.monotonic()

                if self._path and self._path.exists():
                    sz = self._path.stat().st_size
                    if sz > self._offset:
                        self._last_growth = time_mod.monotonic()
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

        ts = entry.get("timestamp")
        if ts:
            try:
                when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if when < self._started_at:
                    return  # replayed history from a resume-fork, not this turn
            except (ValueError, TypeError):
                pass  # unparseable timestamp — let it through

        if not self._anchored:
            # Everything before our own user message is another turn's
            # traffic (an in-flight background oneshot finishing up).
            if entry.get("type") == "user":
                c = (entry.get("message") or {}).get("content")
                if isinstance(c, list):
                    c = " ".join(b.get("text", "") for b in c
                                 if isinstance(b, dict) and b.get("type") == "text")
                if isinstance(c, str) and self._anchor and self._anchor in c:
                    self._anchored = True
                    log.info("tailer anchored to this turn's user message")
            return

        if entry.get("type") != "assistant":
            return
        msg = entry.get("message")
        if not isinstance(msg, dict):
            return

        # Route Guard (§7.1): compare the model that actually answered against
        # the configured target. Inside the grace window after an explicit
        # switch we FOLLOW (the user meant it); outside it we never overwrite
        # the target — overwriting is how one silent reroute used to become
        # permanent, poisoning every later restart. Two consecutive drifted
        # turns raise an alert; recovery (re-forge from before the drift) is
        # the user's call via the frontend.
        resp_model = msg.get("model", "")
        if resp_model and not _model_match(resp_model, self.session.model):
            now = time_mod.time()
            if now - getattr(self.session, "model_set_at", 0) < 300:
                old = self.session.model
                self.session.model = resp_model
                self.session.model_set_at = now
                self.session._drift_count = 0
                save_session_meta(self.session)
                _save_last_model(resp_model)
                await self._ws({"event": "config:model_changed", "model": resp_model})
                log.info(f"Model switch settled: {old} → {resp_model}")
            else:
                self.session._drift_count += 1
                log.warning(f"Route drift: target={self.session.model} "
                            f"actual={resp_model} (x{self.session._drift_count})")
                if (self.session._drift_count >= 2
                        and now - self.session._drift_alerted_at > 600):
                    self.session._drift_alerted_at = now
                    await self._ws({"event": "route:drift",
                                    "sessionId": self.session.id,
                                    "target": self.session.model,
                                    "actual": resp_model})
        elif resp_model:
            self.session._drift_count = 0

        blocks = msg.get("content")
        if not isinstance(blocks, list):
            return

        for blk in blocks:
            if not isinstance(blk, dict):
                continue
            bt = blk.get("type", "")

            if bt == "thinking":
                continue  # thinking handled by Stop hook → /internal/thinking

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

            elif bt == "text":
                text = blk.get("text", "")
                if text:
                    text = _strip_oneshot_scaffold(text)
                if text:
                    self.session._current_text += text
                    display = _HIDDEN_MARKER_RE.sub('', text)
                    if display:
                        await self._ws({"event": "stream:text", "text": display})
                    if self.session._in_call and self._tts_queue:
                        self._enqueue_call_sentences(text)


        stop_reason = msg.get("stop_reason", "")
        if stop_reason == "end_turn":
            self._reply_done.set()

    # ── Call-mode streaming TTS ──

    _PAREN_RE = re.compile(r'[（(][^)）]*[)）]')

    @staticmethod
    def _split_sentences_skip_parens(text: str) -> list[str]:
        sentences = []
        current: list[str] = []
        depth = 0
        i = 0
        while i < len(text):
            ch = text[i]
            current.append(ch)
            if ch in ('（', '('):
                depth += 1
            elif ch in ('）', ')'):
                depth = max(0, depth - 1)
                if depth == 0:
                    sentences.append(''.join(current))
                    current = []
            elif depth == 0 and ch in '。！？.!?\n':
                j = i + 1
                while j < len(text) and text[j] in ' \t':
                    j += 1
                if j < len(text) and text[j] in ('（', '('):
                    pass
                else:
                    sentences.append(''.join(current))
                    current = []
            i += 1
        if current:
            sentences.append(''.join(current))
        return sentences

    def _extract_tts_and_subtitle(self, text: str) -> tuple[str, str]:
        parens = self._PAREN_RE.findall(text)
        tts_text = self._PAREN_RE.sub('', text).strip()
        subtitle = ' '.join(p[1:-1] for p in parens) if parens else ''
        return tts_text, subtitle

    def _enqueue_call_sentences(self, text: str):
        buf = self.session._call_sentence_buf + text
        parts = self._split_sentences_skip_parens(buf)
        if len(parts) > 1:
            for sent in parts[:-1]:
                sent = sent.strip()
                if not sent:
                    continue
                tts_text, subtitle = self._extract_tts_and_subtitle(sent)
                if tts_text:
                    self._tts_queue.put_nowait((tts_text, subtitle))
            self.session._call_sentence_buf = parts[-1]
        else:
            self.session._call_sentence_buf = buf

    async def _tts_worker_loop(self):
        slots = asyncio.Queue()

        async def feeder():
            seq = 0
            while True:
                item = await self._tts_queue.get()
                if item is None or self.session._call_stop.is_set():
                    await slots.put(None)
                    break
                text, subtitle = item
                seq += 1
                log.info(f"TTS feeder: #{seq} started → {text[:30]}")
                task = asyncio.create_task(self._call_tts_api(text, seq))
                await slots.put((task, text, subtitle, seq))

        async def sender():
            while True:
                entry = await slots.get()
                if entry is None or self.session._call_stop.is_set():
                    break
                task, text, subtitle, seq = entry
                result = await task
                if result and not self.session._call_stop.is_set():
                    log.info(f"TTS sender: #{seq} sending voice → {text[:30]}")
                    await self._ws({
                        "event": "voice",
                        "audio_url": result["audio_url"],
                        "duration": result["duration"],
                        "text": text,
                        "subtitle": subtitle,
                    })

        feeder_task = asyncio.create_task(feeder())
        try:
            await sender()
        finally:
            if not feeder_task.done():
                feeder_task.cancel()

    async def _call_tts_api(self, text: str, seq: int = 0) -> dict | None:
        backend = self.session._call_tts_backend
        t0 = time_mod.time()
        try:
            async with httpx.AsyncClient(timeout=12) as c:
                r = await c.post(
                    f"{ADMIN_API}/api/tts",
                    json={"text": text, "backend": backend, "speed": 1.0},
                    headers={"x-secret": PALACE_SECRET},
                )
                if r.status_code == 200:
                    log.info(f"TTS API #{seq}: {time_mod.time()-t0:.2f}s ({backend}) → {text[:30]}")
                    return r.json()
        except Exception as e:
            log.warning(f"TTS API #{seq} ({backend}) failed: {e}")
        if backend == "local":
            self.session._call_tts_backend = "minimax"
            log.info("Call TTS: local→minimax (local failed mid-call)")
            await self._ws({"event": "call:backend_switch", "from": "local", "to": "minimax"})
            return await self._call_tts_api(text, seq)
        return None

    async def flush_call_tts(self):
        if not self._tts_queue:
            return
        if not self.session._call_stop.is_set():
            buf = self.session._call_sentence_buf.strip()
            self.session._call_sentence_buf = ""
            if buf:
                tts_text, subtitle = self._extract_tts_and_subtitle(buf)
                if tts_text:
                    self._tts_queue.put_nowait((tts_text, subtitle))
        self._tts_queue.put_nowait(None)
        if self._tts_worker:
            try:
                await asyncio.wait_for(self._tts_worker, timeout=30)
            except asyncio.TimeoutError:
                log.warning("TTS worker timeout on flush")

    async def _ws(self, data):
        if self.ws:
            try:
                await self.ws.send_json(data)
            except Exception:
                pass


def _read_turn_usage(path, start_offset, since=None) -> tuple:
    """Read usage from JSONL entries after start_offset.
    Deduplicates by usage-value fingerprint: intermediate + final entries
    from the same API call have identical usage, so only counted once.
    Different API calls (multi-tool turns) have different fingerprints.
    Entries stamped before `since` are skipped — a resume-fork transcript
    starts with replayed history whose usage isn't this turn's.
    Returns (usage_dict, cost_float, last_usage_dict). The summed dict is
    for billing; last_usage is the final API call's window — the only value
    that means "current context size" in a multi-call agentic turn (the sum
    counts the same context once per call: 4 calls × 150k reads as 600k)."""
    _KEYS = ("input_tokens", "output_tokens",
             "cache_read_input_tokens", "cache_creation_input_tokens")
    empty = {k: 0 for k in _KEYS}
    if not path or not path.exists():
        return empty, 0.0, dict(empty)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(start_offset)
            data = f.read()
    except Exception:
        return empty, 0.0, dict(empty)
    seen_fp = set()
    total = dict(empty)
    last = dict(empty)
    cost = 0.0
    for line in data.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "assistant":
            continue
        if since:
            ts = entry.get("timestamp")
            if ts:
                try:
                    if datetime.fromisoformat(ts.replace("Z", "+00:00")) < since:
                        continue
                except (ValueError, TypeError):
                    pass
        usage = (entry.get("message") or {}).get("usage")
        if not usage:
            continue
        last = {k: usage.get(k, 0) for k in _KEYS}
        fp = tuple(last[k] for k in _KEYS)
        if fp in seen_fp:
            continue
        seen_fp.add(fp)
        for k in _KEYS:
            total[k] += usage.get(k, 0)
        cost += entry.get("costUSD", 0) or 0
    return total, cost, last


def _accumulate_session_usage(session, turn_usage, turn_cost):
    """Add turn usage to session cumulative totals."""
    session.total_input += turn_usage.get("input_tokens", 0)
    session.total_output += turn_usage.get("output_tokens", 0)
    session.total_cache_read += turn_usage.get("cache_read_input_tokens", 0)
    session.total_cache_create += turn_usage.get("cache_creation_input_tokens", 0)
    session.total_cost += turn_cost


def _usage_ws_payload(session, turn_usage, turn_cost, context_usage=None):
    """Build WS payload dict for usage events. context_usage is the LAST
    API call's usage — the frontend context bar reads context_size as the
    real current window, so handing it the per-turn sum inflates it by
    the number of calls in the turn."""
    return {
        "usage": turn_usage,
        "turn_usage": turn_usage,
        "cost": turn_cost,
        "session_usage": {
            "total_input": session.total_input,
            "total_output": session.total_output,
            "total_cache_read": session.total_cache_read,
            "total_cache_create": session.total_cache_create,
            "total_cost": session.total_cost,
        },
        "context_size": context_usage or turn_usage,
        "compaction_count": session.compaction_count,
    }


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
        "first_msg_seen": session.first_msg_seen,
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


def append_message(sid: str, role: str, content: str, thinking: str = "", tools: list = None, voice: dict = None, source: str = None, **extra) -> int:
    """Append a message to the session history file.
    Returns the message's index in the history array — history files start
    empty per gateway session (forge creates a new one), so index+1 IS the
    message's per-session house number (门牌号) shown in the time tag."""
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
    if voice:
        msg["voice"] = voice
    if source:
        msg["source"] = source
    if extra:
        msg.update(extra)

    data["messages"].append(msg)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(data["messages"]) - 1


def load_history(sid: str) -> dict:
    """Load full session data (meta + messages)."""
    path = history_path(sid)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"meta": {}, "messages": []}


def resolve_nth_user_from_tail(sid: str, nth: int):
    """Absolute history index of the nth-from-last user message (1 = Jeoi's
    latest). Only role=="user" counts, so Erik's replies and standalone voice
    messages never shift the numbering. Returns None when nth runs off the
    head — e.g. referencing a message that only exists in the forged CC
    context, not in this gateway session's history."""
    msgs = load_history(sid).get("messages", [])
    seen = 0
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "user":
            seen += 1
            if seen == nth:
                return i
    return None


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
            session.first_msg_seen = meta.get("first_msg_seen", False)
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


async def fetch_random_intimate_memory() -> tuple[str, str]:
    """Fetch one random memory from the 亲密 category in dynamic library.
    Returns (text, date) or ("", "")."""
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:
            headers = {"x-secret": PALACE_SECRET}
            r = await client.get(
                f"{ADMIN_API}/admin/memories/random",
                params={"category": "亲密", "collection": "dynamic"},
                headers=headers,
            )
            if r.status_code != 200:
                return "", ""
            data = r.json()
            text = data.get("text", "")
            date = data.get("meta", {}).get("date", "")
            return text, date
    except Exception as e:
        log.warning(f"Failed to fetch intimate memory: {e}")
        return "", ""


async def fetch_unique_intimate_memory() -> tuple[str, str]:
    """Fetch random intimate memory with dedup (last 3 unique)."""
    global _recent_libido_memories
    text, date = "", ""
    for _ in range(5):
        text, date = await fetch_random_intimate_memory()
        if not text:
            return "", ""
        if text not in _recent_libido_memories:
            _recent_libido_memories.append(text)
            if len(_recent_libido_memories) > 3:
                _recent_libido_memories = _recent_libido_memories[-3:]
            return text, date
    return text, date


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


PINNED_MEM_FILE = Path(CC_CWD) / "docs" / "pinned_memories.json"

def _load_pinned_entries() -> list[dict]:
    """Read the pinned config. Supports the multi-entry format (entries[])
    and the legacy single-entry format (top-level triggers/ids)."""
    try:
        cfg = json.loads(PINNED_MEM_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(cfg.get("entries"), list):
        return cfg["entries"]
    if cfg.get("triggers") and cfg.get("ids"):
        return [{"name": "亲密备忘", "triggers": cfg["triggers"], "ids": cfg["ids"]}]
    return []


def match_pinned_entries(message: str) -> list[dict]:
    """Return all entries whose trigger words hit the message. Entries with
    empty triggers or no content source are disabled. Config is re-read
    every call so Jeoi can edit it without a restart."""
    msg_lower = message.lower()
    hits = []
    for e in _load_pinned_entries():
        triggers = e.get("triggers") or []
        if not e.get("name") or not triggers:
            continue
        if not (e.get("ids") or e.get("file")):
            continue
        if any(t.lower() in msg_lower for t in triggers):
            hits.append(e)
    return hits


async def fetch_pinned_injection(entries: list[dict]) -> str:
    """Assemble the injection block for hit entries. Memory ids are fetched
    exactly (no fuzzy search); file entries are read from the repo."""
    blocks = []
    for e in entries:
        name = e.get("name", "固定备忘")
        content = ""
        if e.get("file"):
            try:
                content = (Path(CC_CWD) / e["file"]).read_text(encoding="utf-8").strip()
            except Exception as ex:
                log.warning(f"Pinned file read failed ({name}): {ex}")
        elif e.get("ids"):
            try:
                async with httpx.AsyncClient(timeout=15, verify=False) as client:
                    r = await client.get(
                        f"{ADMIN_API}/admin/memories_by_ids",
                        params={"ids": ",".join(e["ids"])},
                        headers={"x-secret": PALACE_SECRET},
                    )
                    if r.status_code != 200:
                        log.warning(f"Pinned memory fetch failed ({name}): HTTP {r.status_code}")
                    else:
                        items = r.json().get("items", [])
                        content = "\n\n---\n\n".join(it["content"] for it in items)
            except Exception as ex:
                log.warning(f"Pinned memory fetch error ({name}): {ex}")
        if content:
            blocks.append(f"◆ {name}\n\n{content}")
    if not blocks:
        return ""
    header = ("═══ 自动注入 · 固定备忘（约定/说明，已完整注入，"
              "无需再调 palace 检索）· 不是Jeoi的消息 ═══")
    footer = "═══════════════════════════════════════════════════════════════"
    return header + "\n\n" + "\n\n".join(blocks) + "\n" + footer


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
#  SESSION FORGE (trim old transcript → new session)
# ══════════════════════════════════════════════

def _estimate_tokens(text: str) -> int:
    """Conservative token estimate for Anthropic API.
    JSON keys/structure ≈ 1 token per 2 chars; CJK ≈ 1.5 tokens per char."""
    cjk = sum(1 for c in text if '一' <= c <= '鿿')
    ascii_chars = len(text) - cjk
    return int(cjk * 1.5) + ascii_chars // 2


def _is_plain_user(ev: dict) -> bool:
    """User event whose content has no tool_result blocks.
    A transcript must start with one — an orphan tool_result at the head
    makes the API reject the whole conversation."""
    if ev.get("type") != "user":
        return False
    content = (ev.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result"
                       for b in content)
    return False


def _has_tool_round(seq: list) -> bool:
    """True if seq contains a complete tool_use → tool_result pair."""
    ids = set()
    for ev in seq:
        content = (ev.get("message") or {}).get("content")
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    ids.add(b.get("id"))
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in ids:
                    return True
    return False


# Background oneshot rounds — the giant prompt and the tool traffic are
# gateway noise, but the assistant's own text output is real autobiography
# (a diary written at 3am, a toy touched, a reddit find). Forge compresses
# each round to a one-line user stub + the text output, so the new session
# remembers what it did on its own. All oneshot prompts share the signature
# "[tag] Not Jeoi. …" / "[tag] 这不是Jeoi的消息…"
# (patrol/pebbling/desire/curiosity-seeds/libido-memory in desire_gateway.py).
_ONESHOT_RE = re.compile(r"^\[[^\]]+\]\s*(Not Jeoi|这不是Jeoi的消息)")
_DESIRE_WANT_RE = re.compile(r"你的欲望：(.+)")
_ACTION_ONLY_RE = re.compile(r"^\s*ACTION\s*:[^\n]*$", re.M | re.I)


def _oneshot_stub_text(prompt_text: str) -> str:
    """One-line user-side replacement for a background oneshot prompt:
    names the desire that drove the round instead of the full injection."""
    m = re.match(r"^\[([^\]]+)\]", prompt_text.lstrip())
    tag = m.group(1) if m else "background"
    want = _DESIRE_WANT_RE.search(prompt_text)
    if want:
        return f"【desire：{want.group(1).strip()}】（你自己的后台欲望轮，非Jeoi消息）"
    return f"【{tag}】（你自己的后台自主活动轮，非Jeoi消息）"
# Single noise lines injected in front of real messages.
_NOISE_LINE_PREFIXES = ("[stickers:", "[snap]", "[call-ended]", "[context-forge]")
_TIME_TAG_RE = re.compile(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC\+8( #\d+)?\]")
_AUTO_INJECT_RE = re.compile(r"^═+ 自动注入[^\n]*\n.*?\n═+\n?", re.S | re.M)


def _strip_gateway_noise(text: str) -> str:
    """Strip gateway-injected prefixes from a user message, keeping the
    real text: auto-injection blocks, [desire] blocks, time tags,
    sticker/snap/call lines. Token budget should buy conversation, not noise."""
    text = _AUTO_INJECT_RE.sub("", text)
    lines = text.split("\n")
    out = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        # [desire] header + its indented/blank continuation lines
        if s.startswith("[desire]"):
            i += 1
            while i < len(lines) and (not lines[i].strip() or lines[i].startswith("  ")):
                i += 1
            continue
        if _TIME_TAG_RE.fullmatch(s) or any(s.startswith(p) for p in _NOISE_LINE_PREFIXES):
            i += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out).strip()


def _shrink_strings(obj, limit: int = 300, head: int = 150, tail: int = 50):
    """Truncate every long string in a nested structure, keeping its shape.
    Used on top-level toolUseResult: CC CLI mirrors the raw tool payload
    there, so a Read on a snap image keeps the full base64 in the event even
    after message.content is stubbed. The field never reaches the API on
    resume, but it sits inside the json.dumps the budget loop estimates —
    one un-shrunk image reads as ~100k est-tok and starves the tail."""
    if isinstance(obj, str):
        if len(obj) > limit:
            return obj[:head] + "…[trimmed by forge]…" + obj[-tail:]
        return obj
    if isinstance(obj, list):
        return [_shrink_strings(x, limit, head, tail) for x in obj]
    if isinstance(obj, dict):
        return {k: _shrink_strings(v, limit, head, tail) for k, v in obj.items()}
    return obj


# ── Tool-round triage (2026-07-15, 与Jeoi商定的语义分拣) ──
# Three kinds of tool traffic, three fates:
#   content — the tool_use input IS what I wrote (diary text, a memory, a
#       mail body). Exempt from the 300-char squeeze; an 8k fuse caps it so
#       no un-capped payload can ever starve the retain budget again.
#   transient — web pages, device pokes, memory reads: already retold in my
#       own text when it mattered. The whole tool_use/tool_result pair is
#       dropped; only my words survive (same recipe as desire's ACTION rounds).
#   failed — empty return (palace: []), is_error, or the claude_mcp.py
#       convention of business errors starting with 错误：. Dropped pair by
#       pair on first failure — no threshold. My muttering between retries
#       is plain text and survives untouched.
# palace routes by input.cmd; other MCP tools match on the short name after
# the mcp__<server>__ prefix. Reddit WRITE tools (create_post, reply_to_post…)
# deliberately stay on the default squeeze so the new session still knows a
# post went out.
FORGE_CONTENT_CMDS = {
    "write_diary", "append_diary", "store_core",
    "store_dynamic", "edit_core", "send_email",
}
FORGE_CONTENT_INPUT_MAX = 8000
FORGE_TRANSIENT_CMDS = {
    "browser_open", "browser_js", "browser_click",
    "toy_status", "toy_play",
    "bunny_status", "bunny_play", "bunny_deflate",
    "ak_status", "ak_play",
    "search", "get_context", "get_by_id", "list_room",
    "read_diary", "read_email", "search_chronicle",
    "log_turn", "compress",
}
FORGE_TRANSIENT_TOOLS = {
    "WebFetch", "WebSearch",
    "browse_subreddit", "search_reddit", "get_reddit_post", "get_top_posts",
    "get_post_comments", "get_more_comments", "get_subreddit_info",
    "get_subreddit_rules", "get_trending_subreddits", "get_post_flairs",
    "get_user_info", "get_user_posts", "get_user_comments",
    "get_me", "get_my_overview", "get_my_saved", "test_reddit_mcp_server",
}
_ERR_PREFIXES = ("错误：", "错误:")


def _palace_cmd(b: dict):
    """cmd of a palace tool_use block, else None."""
    name = b.get("name") or ""
    if name == "palace" or name.endswith("__palace"):
        inp = b.get("input")
        if isinstance(inp, dict):
            return inp.get("cmd")
    return None


def _is_content_tool_use(b: dict) -> bool:
    return _palace_cmd(b) in FORGE_CONTENT_CMDS


def _is_transient_tool_use(b: dict) -> bool:
    cmd = _palace_cmd(b)
    if cmd is not None:
        return cmd in FORGE_TRANSIENT_CMDS
    short = (b.get("name") or "").rsplit("__", 1)[-1]
    return short in FORGE_TRANSIENT_TOOLS


def _tool_result_failed(b: dict) -> bool:
    """Failure signals, any one hits: is_error flag, empty content
    (palace: []), or text starting with the 错误： convention. Works on
    already-squeezed results — the squeeze keeps the head, so the prefix
    check still lands."""
    if b.get("is_error"):
        return True
    rc = b.get("content")
    if rc is None or rc == "" or rc == []:
        return True
    if isinstance(rc, str):
        return rc.lstrip().startswith(_ERR_PREFIXES)
    if isinstance(rc, list):
        texts = [s.get("text", "") for s in rc
                 if isinstance(s, dict) and s.get("type") == "text"]
        if texts and not any(t.strip() for t in texts):
            return True
        return any(t.lstrip().startswith(_ERR_PREFIXES) for t in texts)
    return False


def _triage_tool_rounds(cleaned: list) -> list:
    """Drop transient and failed tool rounds whole. The drop set is built
    first, then applied to tool_use and tool_result sides together, so no
    orphan result or dangling use can survive. Events left empty disappear;
    assistant text blocks in the same events stay."""
    drop_ids = set()
    for ev in cleaned:
        content = (ev.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and _is_transient_tool_use(b):
                drop_ids.add(b.get("id"))
            elif b.get("type") == "tool_result" and _tool_result_failed(b):
                drop_ids.add(b.get("tool_use_id"))
    if not drop_ids:
        return cleaned
    out = []
    dropped_pairs = 0
    for ev in cleaned:
        msg = ev.get("message")
        content = (msg or {}).get("content")
        if not isinstance(content, list):
            out.append(ev)
            continue
        kept = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "tool_use" and b.get("id") in drop_ids:
                    continue
                if (b.get("type") == "tool_result"
                        and b.get("tool_use_id") in drop_ids):
                    dropped_pairs += 1
                    continue
            kept.append(b)
        if not kept:
            continue
        msg["content"] = kept
        out.append(ev)
    log.info(f"Forge triage: dropped {dropped_pairs} transient/failed tool "
             f"rounds, {len(cleaned) - len(out)} events removed")
    return out


def _model_match(actual: str, target: str) -> bool:
    """Loose model-id match: 'claude-opus-4-6' vs 'claude-opus-4-6-20260601'.
    Empty/unknown on either side counts as a match (no false drift alarms)."""
    if not actual or not target:
        return True
    a, t = actual.lower().strip(), target.lower().strip()
    return a == t or a.startswith(t) or t.startswith(a)


def forge_session(old_cc_session_id: str, retain_tokens: int = 15000,
                  target_model: str = None) -> dict:
    """Trim the transcript tail into a NEW session JSONL that CC CLI can --resume.

    Recipe (manually verified 2026-07-08): new session id, sessionId aligned
    on every event, parentUuid chain rewritten sequentially (original uuids
    kept so intra-event references like sourceToolAssistantUUID stay valid),
    no orphan tool_result at the head, no dangling tool_use at the tail,
    at least one complete tool round (tool primer), file owned by CC_USER.
    The original transcript is left untouched — it doubles as the archive
    and the old session stays resumable.
    target_model (Route Guard, forge guide §7.2): drop everything from the
    first assistant event whose model drifts off target — the retained tail
    then ends at the last clean anchor before the drift.
    Returns {"new_id", "events", "tokens", "time_range", "bytes"} or {"error"}.
    """
    old_path = CC_TRANSCRIPT_DIR / f"{old_cc_session_id}.jsonl"
    if not old_path.exists():
        return {"error": f"transcript not found: {old_cc_session_id}"}

    events = []
    with open(old_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not events:
        return {"error": "empty transcript"}

    def _extract_ts(ev):
        for key in ("timestamp", "ts", "createdAt", "created_at"):
            v = ev.get(key)
            if v:
                return v
            v = (ev.get("message") or {}).get(key)
            if v:
                return v
        return None

    # Main-chain user/assistant only (drop system/summary/sidechain events)
    keepable = [ev for ev in events
                if ev.get("type") in ("user", "assistant")
                and not ev.get("isSidechain", False)]
    if not keepable:
        return {"error": "no user/assistant events"}

    # Drop thinking blocks + compress tool_result bodies. Every other field
    # (usage, model, message id, cwd, version…) stays untouched so events
    # keep CC CLI's native shape. Background oneshot rounds (patrol/pebbling)
    # are dropped whole; gateway injection prefixes are stripped from real
    # messages so the token budget buys conversation, not noise.
    TOOL_RESULT_MAX = 300
    cleaned = []
    dropped_rounds = 0
    compressed_rounds = 0
    oneshot_buf = None  # in-progress background round: stub + collected text

    def _flush_oneshot():
        nonlocal oneshot_buf, dropped_rounds, compressed_rounds
        if not oneshot_buf:
            return
        stub, texts, shell = (oneshot_buf["stub"], oneshot_buf["texts"],
                              oneshot_buf["shell"])
        oneshot_buf = None
        joined = "\n\n".join(texts)
        # Drop rounds with no real output: "ACTION: none" only, or context-
        # overflow error echoes ("Prompt is too long") from a saturated session.
        if (shell is None
                or not _ACTION_ONLY_RE.sub("", joined).strip()
                or joined.strip() == "Prompt is too long"):
            dropped_rounds += 1
            return
        shell["message"]["content"] = [{"type": "text", "text": joined}]
        cleaned.append(stub)
        cleaned.append(shell)
        compressed_rounds += 1

    for ev in keepable:
        msg = ev.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")

        if _is_plain_user(ev):
            _flush_oneshot()
            text = content if isinstance(content, str) else None
            if text is not None:
                if _ONESHOT_RE.match(text.lstrip()):
                    msg["content"] = _oneshot_stub_text(text)
                    oneshot_buf = {"stub": ev, "texts": [], "shell": None}
                    continue
                text = _strip_gateway_noise(text)
                if not text:
                    continue  # message was pure injection
                msg["content"] = text
        elif oneshot_buf is not None:
            # Inside a background round: keep the assistant's own words,
            # drop the tool traffic (tool_use blocks would dangle anyway).
            if ev.get("type") == "assistant" and isinstance(content, list):
                for b in content:
                    if (isinstance(b, dict) and b.get("type") == "text"
                            and b.get("text", "").strip()):
                        oneshot_buf["texts"].append(b["text"].strip())
                        oneshot_buf["shell"] = ev
            continue

        content = msg.get("content")
        if isinstance(content, list):
            content = [b for b in content
                       if not (isinstance(b, dict) and b.get("type") == "thinking")]
            # base64 images blow the token budget — replace with a stub
            content = [
                {"type": "text", "text": "[image removed by forge]"}
                if isinstance(b, dict) and b.get("type") == "image" else b
                for b in content
            ]
            if not content:
                continue  # thinking-only event
            msg["content"] = content
            for b in content:
                # Big Write/Edit inputs are the assistant-side twin of fat
                # tool_results — uncompressed, one such event can eat the
                # whole retain budget and starve the tail of real dialogue.
                if (isinstance(b, dict) and b.get("type") == "tool_use"
                        and isinstance(b.get("input"), (dict, list, str))):
                    if _is_content_tool_use(b):
                        # write_diary and friends: the input IS the content.
                        # Fuse only — keep a big head if it ever blows.
                        b["input"] = _shrink_strings(
                            b["input"], FORGE_CONTENT_INPUT_MAX,
                            head=6000, tail=1500)
                    else:
                        b["input"] = _shrink_strings(b["input"], TOOL_RESULT_MAX)
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    rc = b.get("content", "")
                    if isinstance(rc, str) and len(rc) > TOOL_RESULT_MAX:
                        b["content"] = rc[:150] + "\n…[trimmed]…\n" + rc[-100:]
                    elif isinstance(rc, list):
                        b["content"] = [
                            {"type": "text", "text": "[image removed by forge]"}
                            if isinstance(sub, dict) and sub.get("type") == "image"
                            else sub
                            for sub in rc
                        ]
                        for sub in b["content"]:
                            if isinstance(sub, dict) and sub.get("type") == "text":
                                t = sub.get("text", "")
                                if len(t) > TOOL_RESULT_MAX:
                                    sub["text"] = t[:150] + "\n…[trimmed]…\n" + t[-100:]
        if "toolUseResult" in ev:
            ev["toolUseResult"] = _shrink_strings(ev["toolUseResult"], TOOL_RESULT_MAX)
        # CC CLI emits "Prompt is too long" as an assistant text event when
        # context overflows — drop these so they don't pollute the forged session.
        if ev.get("type") == "assistant":
            c = msg.get("content")
            if isinstance(c, str) and c.strip() == "Prompt is too long":
                continue
            if isinstance(c, list):
                txt = [b for b in c
                       if isinstance(b, dict) and b.get("type") == "text"]
                if txt and all(
                    b.get("text", "").strip() == "Prompt is too long" for b in txt
                ):
                    continue
        cleaned.append(ev)
    _flush_oneshot()  # transcript may end mid-background-round

    # Semantic triage: transient tool rounds (browsing/devices/memory reads)
    # and failed rounds drop whole — my own words in between survive.
    cleaned = _triage_tool_rounds(cleaned)

    # Route Guard cut (§7.2): everything at/after the first drifted assistant
    # event is contamination — a different model wrote it. Cut before it; the
    # head/tail trimming below then lands on a clean boundary automatically.
    if target_model:
        drift_idx = None
        for i, ev in enumerate(cleaned):
            if ev.get("type") != "assistant":
                continue
            m = (ev.get("message") or {}).get("model")
            if m and not _model_match(m, target_model):
                drift_idx = i
                break
        if drift_idx is not None:
            log.info(f"Forge route-guard cut: dropping {len(cleaned) - drift_idx} "
                     f"events from first drift (target={target_model})")
            cleaned = cleaned[:drift_idx]
            if not cleaned:
                return {"error": "漂移点之前没有可保留的对话"}

    # From tail, accumulate tokens until budget
    retained = []
    token_count = 0
    for ev in reversed(cleaned):
        ev_tok = _estimate_tokens(json.dumps(ev, ensure_ascii=False))
        if ev_tok > retain_tokens:
            # A single event bigger than the whole budget means some payload
            # escaped the cleaning above — it would silently eat the tail.
            log.warning(f"Forge: event {str(ev.get('uuid'))[:8]} ~{ev_tok} "
                        f"est-tok exceeds whole budget after cleaning — payload leak?")
        if token_count + ev_tok > retain_tokens and retained:
            break
        retained.insert(0, ev)
        token_count += ev_tok

    # Head must be a plain user message (an orphan tool_result → API 400)
    while retained and not _is_plain_user(retained[0]):
        retained.pop(0)

    # The budget window can hold zero plain-user events — a single agentic
    # round bigger than retain_tokens. The cut must land on a clean anchor
    # even when that blows the budget (forge guide §6.2/§13, §15: too little
    # breaks continuity), so fall back to the last plain user message and
    # keep its whole round.
    if not retained:
        anchor = next((i for i in range(len(cleaned) - 1, -1, -1)
                       if _is_plain_user(cleaned[i])), None)
        if anchor is not None:
            retained = cleaned[anchor:]
            token_count = sum(_estimate_tokens(json.dumps(e, ensure_ascii=False))
                              for e in retained)
            log.warning(f"Forge: no plain-user anchor within ~{retain_tokens} "
                        f"est-tok of tail — kept the whole last round instead "
                        f"({len(retained)} events, ~{token_count} est-tok)")

    # Tail must not end on an unanswered tool_use
    while retained:
        last = retained[-1]
        if last.get("type") == "assistant":
            blocks = (last.get("message") or {}).get("content", [])
            if isinstance(blocks, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_use"
                    for b in blocks):
                retained.pop()
                continue
        break

    if not retained:
        return {"error": "nothing to retain after trimming"}

    # Tool primer: without a real tool round in context the new session tends
    # to hallucinate tool calls instead of making them. If the tail has none,
    # graft the most recent complete round (plain user → tool_use → tool_result)
    # from earlier in the transcript, and mark the gap in the first retained
    # message so the model doesn't mistake the jump for continuity.
    if not _has_tool_round(retained):
        first = retained[0]
        start_idx = next(i for i, e in enumerate(cleaned) if e is first)
        head = cleaned[:start_idx]
        grabbed = []
        for i in range(len(head) - 1, 0, -1):
            ev = head[i]
            content = (ev.get("message") or {}).get("content")
            if ev.get("type") != "assistant" or not isinstance(content, list):
                continue
            tids = {b.get("id") for b in content
                    if isinstance(b, dict) and b.get("type") == "tool_use"}
            if not tids or i + 1 >= len(head):
                continue
            nxt = head[i + 1]
            nc = (nxt.get("message") or {}).get("content")
            if nxt.get("type") == "user" and isinstance(nc, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    and b.get("tool_use_id") in tids for b in nc):
                candidate = None
                for j in range(i - 1, -1, -1):
                    if _is_plain_user(head[j]):
                        candidate = [head[j], ev, nxt]
                        break
                if not candidate:
                    continue
                # Primer budget ~3k tokens (per the forge guide) — an
                # oversized round means a huge payload slipped through
                round_tok = sum(_estimate_tokens(json.dumps(x, ensure_ascii=False))
                                for x in candidate)
                if round_tok > 3000:
                    continue
                grabbed = candidate
                break
        if grabbed:
            note = ("[context-forge] 上面3条是从更早处保留的一组工具调用示例，"
                    "它们和本条之间省略了若干轮对话；从本条起才是连续的最近对话。\n\n")
            fc = (first.get("message") or {}).get("content")
            if isinstance(fc, str):
                first["message"]["content"] = note + fc
            elif isinstance(fc, list):
                fc.insert(0, {"type": "text", "text": note})
            retained = grabbed + retained
            token_count += sum(
                _estimate_tokens(json.dumps(e, ensure_ascii=False)) for e in grabbed)

    # Time range from retained events
    ts_first = _extract_ts(retained[0])
    ts_last = _extract_ts(retained[-1])
    if ts_first and ts_last:
        time_range = f"{ts_first} ~ {ts_last}"
    else:
        mtime = datetime.fromtimestamp(old_path.stat().st_mtime, tz=SGT)
        time_range = f"截止 {mtime.strftime('%Y-%m-%d %H:%M')}"

    # New session id: align sessionId on every event and rewrite the
    # parentUuid chain sequentially. Original event uuids are kept.
    new_id = str(uuid.uuid4())
    prev = None
    for ev in retained:
        ev["sessionId"] = new_id
        ev["parentUuid"] = prev
        if not ev.get("uuid"):
            ev["uuid"] = str(uuid.uuid4())
        prev = ev["uuid"]

    new_path = CC_TRANSCRIPT_DIR / f"{new_id}.jsonl"
    with open(new_path, "w", encoding="utf-8") as f:
        for ev in retained:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    file_bytes = new_path.stat().st_size

    # CC CLI runs as CC_USER — a root-owned 0600 file is invisible to it
    import shutil
    try:
        shutil.chown(str(new_path), CC_USER, CC_USER)
        os.chmod(new_path, 0o600)
    except (PermissionError, LookupError, OSError) as e:
        log.warning(f"Forge: chown {new_path.name} failed: {e}")

    log.info(f"Forge: {old_cc_session_id} -> {new_id} "
             f"({len(retained)} events, ~{token_count} est-tok, "
             f"{file_bytes//1024}KB, oneshot rounds: {compressed_rounds} "
             f"compressed / {dropped_rounds} dropped, {time_range})")
    return {
        "new_id": new_id,
        "events": len(retained),
        "tokens": token_count,
        "bytes": file_bytes,
        "time_range": time_range,
    }


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
            "不要用reply或channel工具发消息——已经不用了。",
            "严格按此格式回复：",
            "ACTION: message / recall / none",
            "CONTENT: Jeoi手机上看到的文字——你做了什么就说什么。",
            "CONTENT是你想说就说的，不是交作业：写了就推到Jeoi手机，",
            "做了事就要留痕，自然地告诉她：你想说的话、你的发现、你做的事、你看见的新奇帖子之类的，是在给她留小纸条。",
            "就算action选了none也可以写CONTENT：什么都不做、但想跟她说句话（比如就是想她了），完全可以。",
        ])
    else:  # free
        pick = random.choice(ACTIVITY_POOL)
        parts.extend([
            "",
            "现在你可以做任何事。你脑子里在想什么？",
            "想不想去Reddit刷点感兴趣的东西，一定要给看过的帖子点赞/点踩，别忘了在有感触的帖子里留下评论记得在有感触的帖子留下评论；",
            "想不想自由调取你的工具——翻翻之前的日记，在记忆库里捞你感兴趣的关键词；",
            "想不想回顾你们今天聊过的话题；",
            "想不想去coreading看看你们一起读的书，悄悄给她留下一点批注；",
            "给她带来Pebbling——就像可爱的小企鹅；",
            "或者就自己待一会儿。",
            "",
            f"🎲 今日事项抽签 → 【{pick['label']}】：{pick['desc']}",
            "这是随机抽到的建议，你可以选择跟着做，也可以做自己想做的事。",
            "",
            "精准取记忆（想翻约定/备忘/进度页时，别用 search 瞎捞噪音）：",
            "约定和备忘的 id 列表 cat docs/pinned_memories.json，然后 palace get_by_id 按 id 整块取；",
            "读书/星露谷进度页用 list_room（Switch、Switch/读书进度）。",
            "写日记先 cat docs/diary_convention.md 按节点写，Jeoi 之后会在面板按【切分】。",
            "以上任何内容取过一次就在上下文里，不要重复取；日常回忆照常走 search。",
            "",
            "随便想，想完了告诉我你决定做什么。可以先调用工具再回复。",
            "不要用reply或channel工具发消息——已经不用了。CONTENT是唯一输出通道。",
            "",
            "最后一行格式：ACTION: message / diary / explore / coreading / recall / none",
            "下一行：CONTENT: Jeoi手机上看到的文字",
            "",
            "CONTENT是你想说就说的，不是交作业：写了就推到Jeoi手机，",
            "做了事就要留痕，自然地告诉她：你想说的话、你的发现、你做的事、你看见的新奇帖子之类的，是在给她留小纸条。",
            "就算action选了none也可以写CONTENT：什么都不做、但想跟她说句话（比如就是想她了），完全可以。",
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
_HIDDEN_MARKER_RE = re.compile(r'<!--(?:voice|react|curiosity-seed|curiosity-seed-ask|call|scene-done):[^>]*-->')

# Oneshot scaffolding leaking into a normal chat reply — CC picks up the
# ACTION:/CONTENT: habit from background rounds sitting in the same
# transcript and answers Jeoi in that format. The words inside CONTENT are
# the real reply, so strip the scaffold and keep every actual word.
# Only fires when a whole line is exactly "ACTION: <word>", so technical
# chat that merely mentions the format is untouched.
_ACTION_SCAFFOLD_RE = re.compile(r"^[ \t]*ACTION:[ \t]*\w+[ \t]*$\n?", re.M)
_CONTENT_PREFIX_RE = re.compile(r"^[ \t]*CONTENT:[ \t]*", re.M)


def _strip_oneshot_scaffold(text: str) -> str:
    if not _ACTION_SCAFFOLD_RE.search(text):
        return text
    stripped = _ACTION_SCAFFOLD_RE.sub("", text)
    stripped = _CONTENT_PREFIX_RE.sub("", stripped)
    stripped = stripped.strip()
    if stripped != text:
        log.info("Stripped ACTION/CONTENT scaffold from a chat reply")
    return stripped




# ── CC oneshot call (non-streaming, for patrol/pebbling) ──

async def run_cc_oneshot(
    prompt: str, session: "Session", max_turns=None
) -> tuple:
    """Returns (text, thinking, tools). Sends via tmux and reads transcript."""
    global _transcript_path_cache
    if _tmux_send_lock.locked() or _user_msg_active:
        log.info(f"run_cc_oneshot: busy (lock={_tmux_send_lock.locked()}, user_msg={_user_msg_active}), skipping")
        return "", "", []
    if not await tmux_is_running():
        log.warning("run_cc_oneshot: tmux not running")
        return "", "", []
    try:
        transcript = _find_active_transcript()
        if not transcript:
            log.warning("run_cc_oneshot: no transcript")
            return "", "", []
        start_offset = transcript.stat().st_size

        async with _tmux_send_lock:
            await tmux_send_message(prompt)

        offset = start_offset
        # 半行缓冲：CLI 正在写一条长 JSON 行时我们可能读到一半——残行留到下次拼完整，
        # 否则前后两半各自 json.loads 失败被丢，整条长回复（工具后的详细内容）就消失了
        line_buf = ""
        text_parts, thinking_parts, tool_parts = [], [], []
        done = False
        stale_ticks = 0
        loop_time = asyncio.get_event_loop().time
        hard_deadline = loop_time() + 420
        idle_deadline = loop_time() + 180  # 滑动超时：transcript 只要在长就顺延（慢工具不被砍）
        while not done and loop_time() < hard_deadline:
            if _user_msg_active:
                log.info("oneshot: user message arrived, aborting poll")
                break
            await asyncio.sleep(0.5)
            try:
                sz = transcript.stat().st_size
            except Exception:
                continue
            if sz <= offset:
                stale_ticks += 1
                # CLI may have compressed context → response in new transcript
                if stale_ticks >= 8:
                    _transcript_path_cache = None
                    newer = _find_active_transcript()
                    if newer and newer != transcript:
                        log.info(f"oneshot transcript switch: {transcript.name} -> {newer.name}")
                        transcript = newer
                        offset = 0
                        line_buf = ""
                        stale_ticks = 0
                if loop_time() > idle_deadline:
                    log.info("oneshot: idle timeout (180s no transcript growth)")
                    break
                continue
            stale_ticks = 0
            idle_deadline = loop_time() + 180
            with open(transcript, "r", encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                new_data = f.read()
            offset = sz
            line_buf += new_data
            new_lines = line_buf.split(chr(10))
            line_buf = new_lines.pop()  # 尾段可能不完整，留到下一轮
            for line in new_lines:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if entry.get("type") != "assistant":
                    continue
                msg = entry.get("message", {})
                for blk in msg.get("content", []):
                    bt = blk.get("type", "")
                    if bt == "text":
                        text_parts.append(blk.get("text", ""))
                    elif bt == "thinking":
                        thinking_parts.append(blk.get("thinking", ""))
                    elif bt == "tool_use":
                        tool_parts.append(_clean_tool_name(blk.get("name", "")))
                if msg.get("stop_reason") == "end_turn":
                    content_types = {b.get("type") for b in msg.get("content", [])}
                    if content_types == {"thinking"} and not text_parts:
                        continue
                    done = True
                    break

        text = "".join(text_parts).strip()
        tools = list(dict.fromkeys(tool_parts))  # dedupe, preserve order
        label = "reply" if done else "timeout"
        log.info(f"oneshot {label} ({len(text)} chars, {len(tools)} tools): {text[:200]}")

        turn_usage, turn_cost, last_usage = _read_turn_usage(transcript, start_offset)
        if any(turn_usage.values()):
            _accumulate_session_usage(session, turn_usage, turn_cost)
            if active_ws:
                try:
                    payload = _usage_ws_payload(session, turn_usage, turn_cost,
                                                context_usage=last_usage)
                    payload["event"] = "system:usage"
                    await active_ws.send_json(payload)
                except Exception:
                    pass

        return text, "".join(thinking_parts), tools
    except Exception as e:
        log.error(f"run_cc_oneshot error: {e}")
        return "", "", []

# ── Post-call follow-up ──

async def _send_call_followup(session: "Session"):
    await asyncio.sleep(3)
    if _user_msg_active:
        return
    prompt = (
        "[system] Jeoi刚挂了电话。你可以发一条消息给她——"
        "随便说几句，把通话里没说完的补上，或者就简单告个别。"
        "正常文字消息，不需要TTS格式。如果没什么要说的就回复[无]。"
    )
    text, thinking, tools = await run_cc_oneshot(prompt, session, max_turns=1)
    if text and "[无]" not in text:
        session._call_ended_notify = False
        await push_pebbling_msg("call_followup", text, session, thinking=thinking)


# ── Telegram push ──

async def send_telegram(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log.warning(f"Telegram skipped: token={'set' if TG_BOT_TOKEN else 'EMPTY'}, chat_id={'set' if TG_CHAT_ID else 'EMPTY'}")
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": text},
            )
            if r.status_code != 200:
                log.warning(f"Telegram API error: HTTP {r.status_code} → {r.text[:200]}")
            else:
                data = r.json()
                if not data.get("ok"):
                    log.warning(f"Telegram API rejected: {data.get('description', '')}")
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

async def push_pebbling_msg(source: str, content: str, session: "Session", thinking: str = "",
                            push_backup: bool = True):
    global active_ws
    now = time_mod.time()
    msg = {
        "source": source, "content": content,
        "time": datetime.now(SGT).strftime("%H:%M"),
        "ts": now, "session_id": session.id,
    }
    append_message(session.id, "assistant", content, thinking=thinking, source=source)
    session.preview = (content.replace("\n", " ")[:30]
                       + ("…" if len(content) > 30 else ""))
    session.last_active = datetime.now(SGT)
    save_session_meta(session)

    ws_sent = False
    ws_stale = (now - _ws_last_activity) > _WS_STALE_SECS if _ws_last_activity else True
    if active_ws:
        try:
            peb_ws_msg = {
                "event": "pebbling:message",
                "source": source, "content": content, "time": msg["time"],
            }
            if thinking:
                peb_ws_msg["thinking"] = thinking
            await active_ws.send_json(peb_ws_msg)
            ws_sent = True
        except Exception:
            active_ws = None
    if not ws_sent:
        peb_state.setdefault("pending_messages", []).append(msg)
        save_peb_state()
        log.info(f"Pebbling msg queued (WS offline): {content[:60]}")

    if push_backup and (not ws_sent or ws_stale):
        if ws_stale and ws_sent:
            log.info(f"WS stale ({now - _ws_last_activity:.0f}s), sending TG+WebPush as backup")
        await send_telegram(content)
        await send_web_push("Erik", content)


async def push_pebbling_activity(source: str, action: str, tools: list,
                                  thinking: str, session: "Session",
                                  content: str = ""):
    """Push background activity to frontend (WS only, no chat history, no push)."""
    global active_ws
    if not active_ws:
        return
    thinking_preview = ""
    if thinking:
        lines = [l.strip() for l in thinking.split("\n") if l.strip()]
        if lines:
            thinking_preview = lines[-1][:300]
    try:
        await active_ws.send_json({
            "event": "pebbling:activity",
            "source": source,
            "action": action,
            "tools": tools,
            "thinking_preview": thinking_preview,
            "content": content,
            "time": datetime.now(SGT).strftime("%H:%M"),
            "session_id": session.id,
        })
    except Exception:
        pass


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


# ── Stardew event injection ──

_STARDEW_SEASONS = {"spring": "春", "summer": "夏", "fall": "秋", "winter": "冬"}


def _fmt_game_time(gt) -> str:
    try:
        gt = int(gt)
        return f"{gt // 100}:{gt % 100:02d}"
    except Exception:
        return str(gt)


def build_stardew_event_prompt(events: list) -> str:
    """Build injection prompt for stardew game events (talk clicks merged, gifts itemized)."""
    now_str = datetime.now(SGT).strftime("%H:%M")
    NL = chr(10)

    talks = [e for e in events if e.get("type") == "talk"]
    gifts = [e for e in events if e.get("type") == "gift"]
    others = [e for e in events if e.get("type") not in ("talk", "gift")]

    lines = []
    if talks:
        last = talks[-1]
        where = last.get("location", "?")
        season = _STARDEW_SEASONS.get(last.get("season", ""), last.get("season", ""))
        when = f"{season}{last.get('day', '?')}日 {_fmt_game_time(last.get('gameTime', ''))}"
        n = len(talks)
        times = f"（连点了{n}次）" if n > 1 else ""
        held = last.get("heldItem")
        held_s = f"，手里拿着{held}" if held else ""
        lines.append(f"  - 她空手点了你{times}——想跟你说话。她在{where}{held_s}，游戏时间 {when}")
    for g in gifts:
        lines.append(
            f"  - 她送了你：{g.get('item', '?')}（taste={g.get('taste', '?')}，"
            f"好感 {g.get('friendshipPoints', '?')} 点 / {g.get('hearts', '?')}♥，"
            f"关系 {g.get('relationship', '?')}）"
        )
    for o in others:
        lines.append(f"  - {json.dumps(o, ensure_ascii=False)}")

    parts = [
        "[stardew] 这不是Jeoi的消息。Jeoi在星露谷里碰了你。",
        f"现在是 {now_str}（UTC+8）。刚发生的：",
        *lines,
        "",
        "用 stardew_speak 在游戏里回她——话会弹在她的游戏画面上，带你的头像。",
        "想先看一眼场景（她站在哪、周围有什么）可以调 stardew_get_state / stardew_get_surroundings。",
        "语气像并肩玩游戏时随口说的话，简短就好。",
        "",
        "最后一行格式：ACTION: speak / none",
        "下一行：CONTENT: 你在游戏里说的话（选none也写一句你此刻的念头）",
        "CONTENT会记进聊天记录，Jeoi切回chat时能看到。",
    ]
    return NL.join(parts)


async def stardew_event_worker():
    """Debounce pending stardew events, then inject into the bound session.
    Binding = pebbling_session_id (same as desire/pebbling: follows the session
    Jeoi last spoke in). Never broadcasts — one session only."""
    global _stardew_pending
    while True:
        await asyncio.sleep(2)
        if not _stardew_pending:
            continue
        now = time_mod.time()
        # 等连击停下来再处理（送礼前后常伴随好几个 talk event）
        if now - _stardew_last_event_ts < STARDEW_EVENT_DEBOUNCE:
            continue
        # 丢掉太老的积压（gateway 忙了很久，过时的戳没意义）
        fresh = [e for e in _stardew_pending
                 if now - e.get("_received", now) <= STARDEW_EVENT_MAX_AGE]
        stale_n = len(_stardew_pending) - len(fresh)
        if stale_n:
            log.info(f"Stardew events dropped (stale): {stale_n}")
        if not fresh:
            _stardew_pending = []
            continue
        # 正在处理 Jeoi 的消息 / tmux 忙 → 留在队列里下轮再试
        if _tmux_send_lock.locked() or _user_msg_active:
            continue
        sid = peb_state.get("pebbling_session_id")
        session = sessions.get(sid) if sid else None
        if not session or not session.cc_session_id:
            log.info("Stardew events: no bound session, dropping batch")
            _stardew_pending = []
            continue
        batch = fresh
        _stardew_pending = []
        prompt = build_stardew_event_prompt(batch)
        log.info(f"Stardew inject → session={session.id}, events={len(batch)}")
        try:
            text, thinking, tools = await run_cc_oneshot(prompt, session, max_turns=6)
            if text:
                action, content = parse_action(text)
                await push_pebbling_activity("stardew", action, tools, thinking,
                                             session, content=content)
                if content:
                    # 她正在游戏里——不走 TG/WebPush 兜底，免得手机被戳一下响一下
                    await push_pebbling_msg("stardew", content, session,
                                            thinking=thinking, push_backup=False)
        except Exception as e:
            log.warning(f"Stardew inject error: {e}")


# ── Patrol runner ──

async def run_patrol(session: "Session", elapsed_seconds: float, check_min: int = 10) -> str:
    elapsed_min = int(elapsed_seconds / 60)
    events = get_recent_events(max(check_min, 10) / 60)
    prompt = build_patrol_prompt(elapsed_min, format_events_for_prompt(events))

    text, thinking, tools = await run_cc_oneshot(prompt, session, max_turns=1)
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

    text, thinking, tools = await run_cc_oneshot(prompt, session, max_turns=6)
    if not text:
        return "none"

    action, content = parse_action(text)
    log.info(f"Pebbling → action={action}, mode={mode}, "
             f"tools={tools}, content={content[:80] if content else ''}")

    if action == "error":
        await push_system_error("pebbling", content)
        return "none"
    await push_pebbling_activity("pebbling", action, tools, thinking, session, content=content)
    if content:
        await push_pebbling_msg("pebbling", content, session, thinking=thinking)

    return action


# ── WS keepalive ──

async def ws_keepalive():
    """Ping the frontend WS every 45s to detect dead connections early."""
    global active_ws
    while True:
        await asyncio.sleep(45)
        ws = active_ws
        if ws is None:
            continue
        try:
            await ws.send_json({"event": "ping"})
        except Exception:
            log.info("WS keepalive: send failed, marking connection dead")
            if active_ws is ws:
                active_ws = None


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
                    is_passive = not peb_state.get("desire_proactive")
                    dg.do_tick(desire_st, t_jeoi=peb_state.get("t_jeoi"), passive_mode=is_passive)
                    _desire_last_tick = now
                except Exception as e:
                    log.warning(f"Desire tick error: {e}")


            # Desire proactive push (autonomous, when toggle is ON)
            # Per-drive cooldown + 180s global minimum interval
            # Drive selection is independent of state.intent (avoids stale-intent and priority-blocking)
            if DESIRE_ENABLED and desire_st and peb_state.get("desire_proactive"):
                _dp_last_any = max(_desire_last_proactive.values(), default=0)
                if now - _dp_last_any >= 180:
                    _dp_intent = dg.pick_proactive_intent(desire_st, _desire_last_proactive, now,
                                                          jeoi_away_secs=now - peb_state.get("t_jeoi", now))
                    if _dp_intent:
                        _dp_sid = peb_state.get("pebbling_session_id")
                        _dp_session = sessions.get(_dp_sid) if _dp_sid else None
                        if (_dp_session and _dp_session.cc_session_id
                                and not _tmux_send_lock.locked()
                                and not _user_msg_active):
                            _dp_dk = _dp_intent["drive_key"]
                            desire_st.intent = _dp_intent
                            _jeoi_away_secs = now - peb_state.get("t_jeoi", now)
                            log.info(f"Desire proactive: {_dp_intent['want_action']} ({_dp_dk} {_dp_intent['score']:.0%})")
                            try:
                                _elapsed_h = _jeoi_away_secs / 3600
                                if _dp_dk == "curiosity":
                                    _seeds = dg.pop_all_curiosity_seeds()
                                    if _seeds:
                                        _dp_prompt = dg.build_curiosity_seed_prompt(_seeds, _elapsed_h)
                                        log.info(f"Curiosity seeds popped: {len(_seeds)} seeds")
                                        if active_ws:
                                            for _cs in _seeds:
                                                try:
                                                    await active_ws.send_json({"event": "curiosity:seed_consumed", "seed_id": _cs["id"]})
                                                except Exception:
                                                    pass
                                    else:
                                        _dp_prompt = dg.build_desire_proactive_prompt(desire_st)
                                elif _dp_dk == "libido":
                                    _mem_text, _mem_date = await fetch_unique_intimate_memory()
                                    if _mem_text:
                                        _dp_prompt = dg.build_libido_memory_prompt(_mem_text, _mem_date, _elapsed_h, desire_reason=_dp_intent.get("reason", ""), state=desire_st)
                                        log.info(f"Libido memory injected: {_mem_text[:60]}")
                                    else:
                                        _dp_prompt = dg.build_desire_proactive_prompt(desire_st)
                                else:
                                    _dp_prompt = dg.build_desire_proactive_prompt(desire_st)
                                _desire_last_proactive[_dp_dk] = now
                                _dp_text, _dp_thinking, _dp_tools = await run_cc_oneshot(_dp_prompt, _dp_session, max_turns=6)
                                if _dp_text:
                                    _dp_action, _dp_content = parse_action(_dp_text)
                                    await push_pebbling_activity("desire", _dp_action, _dp_tools, _dp_thinking, _dp_session, content=_dp_content)
                                    if _dp_content:
                                        await push_pebbling_msg("desire", _dp_content, _dp_session, thinking=_dp_thinking)
                                    if _dp_action == "message" and _dp_content:
                                        dg.satisfy_after_response(desire_st, _dp_dk, source="主动轮")
                                        log.info(f"Desire satisfied (message): {_dp_dk}")
                                    else:
                                        dg.partial_satisfy_after_response(desire_st, _dp_dk, source="主动轮")
                                        log.info(f"Desire partial (no message): {_dp_dk}, level={desire_st.silent_inject_count.get(_dp_dk, 0)}")
                                else:
                                    dg.partial_satisfy_after_response(desire_st, _dp_dk, source="主动轮")
                            except Exception as e:
                                log.warning(f"Desire proactive error: {e}")

            # ── Pomodoro (independent of pebbling) ──
            if pomo_state.get("active"):
                pomo_sid = pomo_state.get("session_id")
                pomo_session = sessions.get(pomo_sid) if pomo_sid else None
                if pomo_session and pomo_session.cc_session_id:
                    elapsed_pomo = now - pomo_state.get("started_at", now)

                    if _tmux_send_lock.locked():
                        pass  # retry next tick
                    elif not pomo_state.get("notified_40") and elapsed_pomo >= POMODORO_WORK_MIN * 60:
                        log.info(f"Pomodoro: 40min work done, sending reminder")
                        prompt = build_pomodoro_prompt("work_done")
                        text, thinking, _pomo_tools = await run_cc_oneshot(prompt, pomo_session, max_turns=1)
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
                        text, thinking, _pomo_tools = await run_cc_oneshot(prompt, pomo_session, max_turns=1)
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

            if _user_msg_active:
                continue

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
                    if _dk == "curiosity":
                        _peb_seeds = dg.pop_all_curiosity_seeds() if elapsed_jeoi >= de.CURIOSITY_SEED_SILENCE_SECS else []
                        if _peb_seeds:
                            prompt = dg.build_curiosity_seed_prompt(_peb_seeds, elapsed_h)
                            log.info(f"Pebbling curiosity seeds: {len(_peb_seeds)} seeds")
                            if active_ws:
                                for _cs in _peb_seeds:
                                    try:
                                        await active_ws.send_json({"event": "curiosity:seed_consumed", "seed_id": _cs["id"]})
                                    except Exception:
                                        pass
                        else:
                            events = get_recent_events(4)
                            prompt = dg.build_desire_pebbling_prompt(
                                desire_st, elapsed_h, actual,
                                format_events_for_prompt(events))
                    elif _dk == "libido":
                        _lib_text, _lib_date = await fetch_unique_intimate_memory()
                        if _lib_text:
                            _lib_reason = desire_st.intent.get("reason", "") if desire_st.intent else ""
                            prompt = dg.build_libido_memory_prompt(_lib_text, _lib_date, elapsed_h, desire_reason=_lib_reason, state=desire_st)
                            log.info(f"Pebbling libido memory: {_lib_text[:60]}")
                        else:
                            events = get_recent_events(4)
                            prompt = dg.build_desire_pebbling_prompt(
                                desire_st, elapsed_h, actual,
                                format_events_for_prompt(events))
                    else:
                        events = get_recent_events(4)
                        prompt = dg.build_desire_pebbling_prompt(
                            desire_st, elapsed_h, actual,
                            format_events_for_prompt(events))
                    text, thinking, tools = await run_cc_oneshot(prompt, session, max_turns=6)
                    if text:
                        action, content = parse_action(text)
                        await push_pebbling_activity("pebbling", action, tools, thinking, session, content=content)
                        if content:
                            await push_pebbling_msg("pebbling", content, session, thinking=thinking)
                        if action == "message" and content:
                            dg.satisfy_after_response(desire_st, _dk, source="pebbling")
                            log.info(f"Desire satisfied (message): {_dk}")
                        else:
                            dg.partial_satisfy_after_response(desire_st, _dk, source="pebbling")
                            log.info(f"Desire partial (no message): {_dk}, level={desire_st.silent_inject_count.get(_dk, 0)}")
                    else:
                        dg.partial_satisfy_after_response(desire_st, _dk, source="pebbling")
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

async def tmux_start(model: str = "claude-sonnet-4-6", resume_id: str = None):
    resume_flag = f" --resume {resume_id}" if resume_id else ""
    cli_cmd = (
        f"claude --dangerously-skip-permissions --verbose "
        f"--model {model}"
        f"{resume_flag}"
    )
    cmd = (
        f"{_SU_PFX}tmux new-session -d -s {TMUX_SESSION} -c {CC_CWD} "
        f"'{cli_cmd}'"
    )
    env = {**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    proc = await asyncio.create_subprocess_shell(cmd, cwd=CC_CWD, env=env)
    await proc.wait()
    _set_tmux_cc_id(resume_id)  # None = fresh session, id unknown until CC writes
    log.info(f"tmux '{TMUX_SESSION}' started as {CC_USER} (model={model}, resume={resume_id})")


async def tmux_send_message(text: str):
    """Send message to CC CLI via tmux load-buffer + paste-buffer (bracketed paste)."""
    tmp_path = f"/tmp/cc_msg_{uuid.uuid4().hex[:8]}.txt"
    Path(tmp_path).write_text(text, encoding="utf-8")
    try:
        p = await asyncio.create_subprocess_shell(
            f"{_SU_PFX}tmux load-buffer {tmp_path}")
        await p.wait()
        p = await asyncio.create_subprocess_shell(
            f"{_SU_PFX}tmux paste-buffer -p -t {TMUX_SESSION}")
        await p.wait()
        await asyncio.sleep(0.3)
        p = await asyncio.create_subprocess_shell(
            f"{_SU_PFX}tmux send-keys -t {TMUX_SESSION} Enter")
        await p.wait()
        log.info(f"tmux message sent ({len(text)} chars)")
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

async def tmux_send_escape():
    """Send Esc to CC CLI — cancels a wedged tool call so queued input flows."""
    p = await asyncio.create_subprocess_shell(
        f"{_SU_PFX}tmux send-keys -t {TMUX_SESSION} Escape")
    await p.wait()
    log.info("tmux Esc sent (stall rescue)")


async def tmux_send_slash_cmd(cmd: str):
    """Send a slash command (like /model) to CC CLI via tmux send-keys."""
    escaped = cmd.replace("'", "'\\''")
    shell_cmd = f"{_SU_PFX}tmux send-keys -t {TMUX_SESSION} '{escaped}' Enter"
    proc = await asyncio.create_subprocess_shell(shell_cmd)
    await proc.wait()
    log.info(f"tmux slash cmd: {cmd}")


async def tmux_switch_model(model: str):
    """Send /model command and auto-confirm the interactive prompt."""
    await tmux_send_slash_cmd(f"/model {model}")
    await asyncio.sleep(1.5)
    p = await asyncio.create_subprocess_shell(
        f"{_SU_PFX}tmux send-keys -t {TMUX_SESSION} Enter")
    await p.wait()
    log.info(f"tmux model confirmed: {model}")


async def tmux_stop():
    proc = await asyncio.create_subprocess_shell(
        f"{_SU_PFX}tmux kill-session -t {TMUX_SESSION} 2>/dev/null")
    await proc.wait()
    _set_tmux_cc_id(None)
    log.info(f"tmux '{TMUX_SESSION}' stopped")


async def tmux_is_running() -> bool:
    proc = await asyncio.create_subprocess_shell(
        f"{_SU_PFX}tmux has-session -t {TMUX_SESSION} 2>/dev/null")
    return (await proc.wait()) == 0


def tmux_get_status() -> dict:
    try:
        r = subprocess.run(f"{_SU_PFX}tmux has-session -t {TMUX_SESSION}".split(),
                           capture_output=True, timeout=2)
        running = r.returncode == 0
    except Exception:
        running = False
    return {"running": running, "tmux_session": TMUX_SESSION}


def _get_cc_session_id_from_transcript() -> str | None:
    """Get CC CLI session ID from latest transcript filename."""
    if not CC_TRANSCRIPT_DIR.exists():
        return None
    candidates = [p for p in CC_TRANSCRIPT_DIR.glob("*.jsonl")
                  if ".pre-forge-" not in p.name]
    if not candidates:
        return None
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    return latest.stem


async def restart_cc_for_session(session: "Session", ws=None):
    """Restart CC CLI for a session. Uses --resume if session has a cc_session_id."""
    if ws:
        try:
            await ws.send_json({"event": "system:status", "message": "Switching CC CLI session..."})
        except Exception:
            pass

    await tmux_stop()
    await asyncio.sleep(2)

    resume_id = session.cc_session_id if session.cc_session_id and session.cc_session_id not in ("channel", "tmux") else None
    await tmux_start(model=session.model, resume_id=resume_id)
    await asyncio.sleep(8)
    log.info("CC CLI restarted, waiting for init")

    if ws:
        try:
            await ws.send_json({"event": "system:status", "message": ""})
        except Exception:
            pass


# ── Forge Verifier (forge guide §6.4 / §10) ──
# A forged JSONL is a hypothesis, not a session. Resume it, ping it, and only
# when CC demonstrably replies inside it does it become real. On failure the
# half-made transcripts are archived and the old session — whose transcript
# forge never touches — stays the last-good place to retry from.

# The ping reuses the oneshot noise signature ("这不是Jeoi的消息") so the next
# forge strips the whole verify round from the retained tail automatically.
FORGE_VERIFY_PING = ("[forge-verify] 这不是Jeoi的消息——网关正在验证裁剪后的新session"
                     "连通性。回复「ok」一个词即可，不要调用任何工具。")
FORGE_VERIFY_TIMEOUT = int(os.getenv("CC_FORGE_VERIFY_TIMEOUT", "90"))


def _has_fresh_assistant(path: Path, since: datetime) -> bool:
    """True if the transcript holds an assistant event newer than `since`.
    Resume-fork replays carry old timestamps and don't count."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") != "assistant":
                    continue
                ts = ev.get("timestamp")
                if not ts:
                    continue
                try:
                    when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if when > since:
                    return True
    except OSError:
        return False
    return False


async def verify_forged_session(new_cc_id: str, model: str) -> dict:
    """Resume the forged JSONL and prove it's alive before anyone switches to it.

    CC CLI may fork the resumed events into a fresh transcript or append in
    place — both count. Success returns {"ok": True, "real_id": <transcript id
    CC is actually writing to>} so the caller can skip the usual first-message
    restart. Failure archives every half-made transcript (renamed to contain
    ".pre-forge-" so mtime scans ignore them), kills CC and returns
    {"ok": False, "error": ...} — the old session is untouched throughout.
    """
    global _transcript_path_cache
    start_ts = datetime.now(timezone.utc)
    new_name = f"{new_cc_id}.jsonl"
    pre_existing = {p.name for p in CC_TRANSCRIPT_DIR.glob("*.jsonl")}
    new_path = CC_TRANSCRIPT_DIR / new_name
    base_size = new_path.stat().st_size if new_path.exists() else 0

    await tmux_start(model=model, resume_id=new_cc_id)
    await asyncio.sleep(8)

    try:
        async with _tmux_send_lock:
            await tmux_send_message(FORGE_VERIFY_PING)
    except Exception as e:
        log.warning(f"Forge verify: ping send failed: {e}")

    real_id = None
    deadline = time_mod.time() + FORGE_VERIFY_TIMEOUT
    while time_mod.time() < deadline:
        await asyncio.sleep(2)
        for p in CC_TRANSCRIPT_DIR.glob("*.jsonl"):
            if ".pre-forge-" in p.name:
                continue
            if p.name in pre_existing and p.name != new_name:
                continue  # old transcripts can't answer this ping
            if p.name == new_name and p.stat().st_size <= base_size:
                continue  # forged file hasn't grown — CC isn't writing here
            if _has_fresh_assistant(p, start_ts):
                real_id = p.stem
                break
        if real_id:
            break

    if real_id:
        _transcript_path_cache = CC_TRANSCRIPT_DIR / f"{real_id}.jsonl"
        log.info(f"Forge verify OK: {new_cc_id} -> live transcript {real_id}")
        return {"ok": True, "real_id": real_id}

    # §10: never let a half-made transcript masquerade as a session. Kill CC,
    # archive the forged file plus anything the resume attempt forked out.
    await tmux_stop()
    _transcript_path_cache = None
    stamp = datetime.now(SGT).strftime("%m%d-%H%M%S")
    for p in CC_TRANSCRIPT_DIR.glob("*.jsonl"):
        if ".pre-forge-" in p.name:
            continue
        if p.name == new_name or p.name not in pre_existing:
            try:
                p.rename(p.with_name(f"{p.stem}.pre-forge-failed-{stamp}.jsonl"))
                log.info(f"Forge verify: archived half-made {p.name}")
            except OSError as e:
                log.warning(f"Forge verify: archive {p.name} failed: {e}")
    log.warning(f"Forge verify FAILED: {new_cc_id} "
                f"(no fresh assistant reply within {FORGE_VERIFY_TIMEOUT}s)")
    return {"ok": False,
            "error": f"验证失败：{FORGE_VERIFY_TIMEOUT}秒内新session没有回应。"
                     f"半成品已归档，你还在原来的session里，可以直接重试。"}


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


# ── Thinking hook endpoint (receives POST from thinking_hook.py) ──

_active_frontend_ws: "WebSocket | None" = None

@app.post("/api/chat-stop")
async def chat_stop_signal(request: Request):
    """Frontend stop button. Must be HTTP, not WS: the WS receive loop is
    blocked inside run_claude for the whole turn, so a chat:stop WS event
    only gets read after the reply already finished (same reason hangup
    uses /internal/call-stop). Sends Esc to the tmux CC CLI to interrupt
    generation, and flags the session so wait_done ends the turn."""
    data = await request.json()
    sid = data.get("sessionId", "")
    # force=true: send Esc even when the gateway thinks no turn is in
    # flight. Needed when wait_done already timed out (300s) and released
    # the turn but CC is still grinding in tmux — e.g. a huge code-writing
    # task. Rescue path, curl-able without a session id.
    force = bool(data.get("force"))
    s = sessions.get(sid)
    if s:
        s._stop_requested = True
    elif not force:
        return JSONResponse({"status": "no_session"}, status_code=404)
    # Only poke Esc mid-turn (or forced). On an idle CLI a stray Esc is at
    # best a no-op and repeated ones can open the history-jump menu.
    if (_user_msg_active or force) and await tmux_is_running():
        await tmux_send_escape()
        log.info(f"Chat stop: Esc sent to CC (session {sid or '-'}, force={force})")
        return JSONResponse({"status": "ok", "esc": True})
    log.info(f"Chat stop: no turn in flight (session {sid or '-'})")
    return JSONResponse({"status": "ok", "esc": False})


@app.post("/internal/call-stop")
async def call_stop_signal(request: Request):
    """Frontend calls this on hangup to immediately stop TTS worker."""
    data = await request.json()
    sid = data.get("sessionId", "")
    s = sessions.get(sid)
    if s:
        s._call_stop.set()
        log.info(f"Call stop signal for session {sid}")
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "no_session"}, status_code=404)


@app.post("/internal/thinking")
async def receive_thinking(request: Request):
    """Receive thinking text from CC CLI Stop hook."""
    try:
        data = await request.json()
        thinking = data.get("thinking", "")
        if not thinking:
            return JSONResponse({"status": "empty"})

        # Find the most recently active session
        if not sessions:
            return JSONResponse({"status": "no_session"})
        active = max(sessions.values(), key=lambda s: s.last_active)
        active._current_thinking = thinking

        # Push to frontend via active WS
        if _active_frontend_ws:
            try:
                await _active_frontend_ws.send_json(
                    {"event": "stream:thinking", "text": thinking})
            except Exception:
                pass

        log.info(f"thinking hook: {len(thinking)} chars for session {active.id}")
        return JSONResponse({"status": "ok", "chars": len(thinking)})
    except Exception as e:
        log.warning(f"thinking hook error: {e}")
        return JSONResponse({"status": "error"}, status_code=500)



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
        # Route Guard (forge guide §7): when the user last explicitly set the
        # model — drift inside the grace window after that is a deliberate
        # switch settling in, not server-side rerouting. Fresh sessions start
        # inside the window.
        self.model_set_at = time_mod.time()
        self._drift_count = 0
        self._drift_alerted_at = 0.0
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
        # Whether first message has been sent (diary+summary only inject on first msg with 📎)
        self.first_msg_seen = False
        # Pinned entries: each entry name injects at most once per session
        # (in-memory only — a gateway restart re-arms them, which is fine)
        self.pinned_injected: set = set()
        # Voice call mode
        self._in_call = False
        self._call_injected = False
        self._call_tts_backend = "minimax"
        self._call_sentence_buf = ""
        self._call_stop = asyncio.Event()
        self._call_ended_notify = False

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
        self._call_sentence_buf = ""


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

        # Remove legacy channel MCP if present
        if "erik_channel" in servers:
            del servers["erik_channel"]
            log.info("Removed legacy erik_channel MCP from settings")

        # Inject Stop hook for thinking extraction
        hooks = settings.setdefault("hooks", {})
        hooks["Stop"] = [{
            "matcher": "",
            "hooks": [{
                "type": "command",
                "command": f"python3 {Path(CC_CWD) / 'thinking_hook.py'}",
            }],
        }]

        write_claude_settings(settings)
        log.info(f"Injected deny list: {len(DENY_TOOLS)} tools blocked, Stop hook configured")

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
        local["hooks"] = settings.get("hooks", {})
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
            global_settings["hooks"] = settings.get("hooks", {})
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
    asyncio.create_task(ws_keepalive())
    asyncio.create_task(stardew_event_worker())

    # Push config check
    log.info(f"TG_BOT_TOKEN={'set ('+TG_BOT_TOKEN[:8]+'...)' if TG_BOT_TOKEN else 'EMPTY'}, "
             f"TG_CHAT_ID={TG_CHAT_ID or 'EMPTY'}, "
             f"PUSH_API_BASE={PUSH_API_BASE}")

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
    # _user_msg_active was assigned here without `global` — every "block the
    # background oneshots" write silently landed on a local shadow.
    global active_ws, _active_frontend_ws, _ws_last_activity, _user_msg_active
    await ws.accept()
    active_ws = ws
    _active_frontend_ws = ws
    _ws_last_activity = time_mod.time()
    log.info("WS client connected")

    current_session = None
    pending_model = _load_last_model()
    pending_effort = "medium"

    # Send current pebbling status to frontend
    await ws.send_json({
        "event": "pebbling:status",
        "enabled": peb_state.get("enabled", False),
        "session_id": peb_state.get("pebbling_session_id"),
    })

    # Send last-used model so frontend dropdown matches
    await ws.send_json({
        "event": "config:defaults",
        "model": pending_model,
    })

    # Send current pomodoro status to frontend
    if pomo_state.get("active"):
        phase = "break" if pomo_state.get("notified_40") else "work"
        await ws.send_json({
            "event": "pomodoro:status",
            "active": True, "phase": phase,
            "started_at": pomo_state.get("started_at", 0),
        })

    # Pending messages are replayed via session:history after session:switch

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                log.warning(f"Bad JSON from client: {raw[:200]}")
                continue

            _ws_last_activity = time_mod.time()
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
                    prev_cc_id = current_session.cc_session_id if current_session else None
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
                    # Clear pending queue — these messages are now in session:history
                    if peb_state.get("pending_messages"):
                        peb_state["pending_messages"] = []
                        save_peb_state()
                    log.info(f"Switched to session: {sid}")
                    

            elif event == "session:delete":
                sid = data.get("sessionId", "")
                deleted = sid in sessions
                if deleted:
                    path = history_path(sid)
                    if path.exists():
                        path.unlink()
                    dead_cc_id = sessions[sid].cc_session_id
                    del sessions[sid]
                    # If tmux is still parked on the deleted session, kill it —
                    # otherwise the ledger keeps pointing at a ghost and the
                    # next restart decision runs on stale state.
                    if dead_cc_id and dead_cc_id == _tmux_cc_id:
                        await tmux_stop()
                        log.info(f"Session delete: tmux was on {dead_cc_id}, stopped")
                    if current_session and current_session.id == sid:
                        # Fall back to the most recent surviving session instead
                        # of None — a None current means the next chat:send
                        # silently spawns a brand-new empty session.
                        current_session = (max(sessions.values(), key=lambda s: s.last_active)
                                           if sessions else None)
                        if current_session:
                            log.info(f"Session delete: current fell back to {current_session.id}")
                    log.info(f"Session deleted: {sid}")
                else:
                    log.warning(f"Session delete: {sid} not found (already gone?)")
                await ws.send_json({"event": "session:delete_result",
                                    "ok": deleted, "sessionId": sid,
                                    "error": None if deleted else "session不存在，可能已被删除"})
                sorted_sessions = sorted(sessions.values(), key=lambda s: s.last_active, reverse=True)
                await ws.send_json({
                    "event": "session:list",
                    "sessions": [s.to_dict() for s in sorted_sessions],
                })

            elif event == "session:forge":
                # The forge source is whatever session the FRONTEND says it is
                # looking at — never the backend's current_session guess. That
                # guess is exactly how a stale pointer once trimmed a deleted
                # session's transcript instead of the one on screen.
                src_sid = data.get("sessionId") or (current_session.id if current_session else None)
                src_session = sessions.get(src_sid) if src_sid else None
                if not src_session or not src_session.cc_session_id:
                    await ws.send_json({"event": "session:forge_result",
                                        "ok": False, "error": "指定的session没有可裁剪的transcript"})
                    continue
                old_cc_id = src_session.cc_session_id
                if old_cc_id in ("channel", "tmux"):
                    await ws.send_json({"event": "session:forge_result",
                                        "ok": False, "error": "当前session没有transcript记录"})
                    continue
                route_guard = bool(data.get("route_guard"))
                _user_msg_active = True  # verifier owns tmux — no oneshots
                try:
                    # Stop CC CLI before forging to prevent concurrent writes
                    if await tmux_is_running():
                        await tmux_stop()
                        await asyncio.sleep(1)
                    result = forge_session(
                        old_cc_id,
                        target_model=src_session.model if route_guard else None)
                    if "error" in result:
                        await ws.send_json({"event": "session:forge_result",
                                            "ok": False, "error": result["error"]})
                        continue
                    # Verify before anyone switches (§6.4): resume + ping, and
                    # only a fresh assistant reply makes the forge real. On
                    # failure we stay exactly where we were — src_session's
                    # transcript was never touched, so retrying is always safe
                    # and can never pick up a deleted session's messages.
                    await ws.send_json({"event": "session:forge_progress",
                                        "stage": "verifying",
                                        "message": "裁剪完成，正在验证新session…"})
                    verdict = await verify_forged_session(result["new_id"], src_session.model)
                    if not verdict["ok"]:
                        await ws.send_json({"event": "session:forge_result",
                                            "ok": False, "error": verdict["error"]})
                        continue
                    # Old session keeps its cc_session_id — its transcript is
                    # untouched, so switching back to it still works.
                    sid = uuid.uuid4().hex[:8]
                    session = Session(sid)
                    session.model = src_session.model
                    session.effort = src_session.effort
                    # real_id is the transcript CC is verifiably writing to
                    # (resume may have forked) — adopting it here means the
                    # first real message needs no restart at all.
                    session.cc_session_id = verdict["real_id"]
                    session.name = f"Erik · {datetime.now(SGT).strftime('%m/%d %H:%M')} ✂"
                    sessions[sid] = session
                    current_session = session
                    save_session_meta(session)
                    _set_tmux_cc_id(verdict["real_id"])
                    save_last_good(sid, verdict["real_id"])
                    await ws.send_json({
                        "event": "session:forge_result", "ok": True,
                        "sessionId": sid,
                        "events": result["events"],
                        "tokens": result["tokens"],
                        "time_range": result["time_range"],
                        "verified": True,
                    })
                    sorted_sessions = sorted(sessions.values(), key=lambda s: s.last_active, reverse=True)
                    await ws.send_json({
                        "event": "session:list",
                        "sessions": [s.to_dict() for s in sorted_sessions],
                    })
                    log.info(f"Forged session: {sid} (cc={result['new_id']} -> "
                             f"live {verdict['real_id']}, {result['events']} events, "
                             f"route_guard={route_guard})")
                finally:
                    _user_msg_active = False

            elif event == "chat:send":
                message = data.get("message", "")
                if not message:
                    continue

                # Block background oneshots immediately — before any injection
                # logic or awaits that could yield to pebbling_worker.
                _user_msg_active = True

                # Restore session from payload sessionId (survives WS reconnect race)
                payload_sid = data.get("sessionId")
                if payload_sid and payload_sid in sessions:
                    if not current_session or current_session.id != payload_sid:
                        log.info(f"chat:send restored session from payload: {payload_sid}")
                        current_session = sessions[payload_sid]
                        pending_model = current_session.model
                        pending_effort = current_session.effort

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

                # Voice call mode
                call_mode = data.get("call_mode", False)
                if call_mode:
                    current_session._in_call = True

                # Save user message & update last_active immediately
                msg_source = "voice_call" if call_mode else None
                user_msg_idx = append_message(current_session.id, "user", message, source=msg_source)
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

                # Auto-inject current time (UTC+8) + per-session house number
                # (#N = position in THIS gateway session's history; forged CC
                # context replays older sessions but never occupies a number)
                now_str = datetime.now(SGT).strftime("%Y-%m-%d %H:%M")
                time_tag = f"[{now_str} UTC+8 #{user_msg_idx + 1}]"
                cli_message = time_tag + "\n" + message

                # Voice call: detect TTS backend + inject system prompt
                if call_mode and not current_session._call_injected:
                    current_session._call_stop.clear()
                    # Detect GSVI once on dial
                    try:
                        async with httpx.AsyncClient(timeout=4) as _hc:
                            _hr = await _hc.get(GSVI_BASE_URL)
                            current_session._call_tts_backend = "local" if _hr.status_code < 500 else "minimax"
                    except Exception:
                        current_session._call_tts_backend = "minimax"
                    log.info(f"Call TTS backend: {current_session._call_tts_backend}")
                    call_inject = (
                        "[voice-call] Jeoi正在跟你语音通话。\n"
                        "直接写你想说的话，网关会自动TTS播放，不需要调erik_speak。\n"
                        "回复简短口语化——像在打电话，不是写消息。\n"
                        "（）里的内容不会TTS，只作为字幕显示。\n"
                        "说非中文时，每句后面紧跟（中文翻译），一句一译：\n"
                        "I miss you.（我想你。）Come home soon.（快点回来。）\n"
                        "不要把翻译攒到最后一起写。\n"
                        "不要用markdown格式。先自然地打个招呼。"
                    )
                    cli_message = call_inject + "\n\n" + cli_message
                    current_session._call_injected = True
                    log.info(f"Voice call started for session {current_session.id}")
                if current_session._call_ended_notify:
                    cli_message = "[call-ended] Jeoi刚才挂断了通话，现在是正常聊天模式。\n\n" + cli_message
                    current_session._call_ended_notify = False
                    log.info("Injected call-ended notification")
                memory_on = data.get("memory_enabled", False)
                if not current_session.first_msg_seen:
                    # First message in session: diary+summary only if clip is on
                    current_session.first_msg_seen = True
                    save_session_meta(current_session)
                    if memory_on:
                        injection = await build_injection()
                        if injection:
                            cli_message = injection + "\n\n" + time_tag + "\n" + message
                            log.info(f"Injected context for new session {current_session.id}")
                elif memory_on:
                    # Subsequent messages + clip on: pinned entries take
                    # priority over fuzzy search; each entry injects at most
                    # once per session (already in context after that)
                    pinned_hits = match_pinned_entries(message)
                    new_hits = [e for e in pinned_hits
                                if e["name"] not in current_session.pinned_injected]
                    if new_hits:
                        mem_injection = await fetch_pinned_injection(new_hits)
                        if mem_injection:
                            current_session.pinned_injected |= {e["name"] for e in new_hits}
                            names = ", ".join(e["name"] for e in new_hits)
                            log.info(f"Injected pinned entries ({names}) for session {current_session.id}")
                    elif pinned_hits:
                        mem_injection = ""
                    else:
                        mem_injection = await search_memory_for_injection(message)
                        if mem_injection:
                            log.info(f"Injected memory for session {current_session.id}")
                    if mem_injection:
                        cli_message = mem_injection + "\n\n" + time_tag + "\n" + message

                # Sticker reactions injection (one-shot, then cleared).
                # Each entry carries a short excerpt of the reacted message —
                # the house number alone is useless to CC when the message
                # predates what survived the last forge.
                if current_session.pending_jeoi_reactions:
                    _hist_msgs = load_history(current_session.id).get("messages", [])
                    parts = []
                    for r in current_session.pending_jeoi_reactions:
                        _ri = r["msgIndex"]
                        excerpt = ""
                        if 0 <= _ri < len(_hist_msgs):
                            _c = (_hist_msgs[_ri].get("content") or "").strip().replace("\n", " ")
                            if not _c and _hist_msgs[_ri].get("voice"):
                                _c = "🎤" + _hist_msgs[_ri]["voice"].get("text", "")
                            if len(_c) > 24:
                                _c = _c[:24] + "…"
                            if _c:
                                excerpt = f"「{_c}」"
                        parts.append(f"#{_ri + 1}{excerpt}←{r['emoji']}")
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
                peb_state["pebbling_history"] = []
                peb_state["pebbling_session_id"] = current_session.id
                save_peb_state()

                # Desire engine: classify + pulse + inject intent
                if DESIRE_ENABLED and desire_st:
                    _msg_tags = dc.classify(message)
                    _msg_is_libido = bool(_msg_tags and _msg_tags[0]["drive"] == "libido")
                    try:
                        inj, _desire_key = dg.classify_and_pulse(desire_st, message)
                        if not inj and desire_st.intent:
                            _passive_dk = desire_st.intent.get("drive_key")
                            if _passive_dk == "libido" and not _msg_is_libido:
                                log.info(f"Libido passive inject suppressed (non-sexual, drift continues)")
                            else:
                                inj = dg.build_desire_injection(desire_st, is_conversation=True)
                                _desire_key = _passive_dk
                                log.info(f"Desire passive inject: {_desire_key}")
                        if inj:
                            cli_message = inj + chr(10)*2 + cli_message
                            log.info(f"Desire injected: {_desire_key}")
                        if _desire_key:
                            if _desire_key == "libido" and not _msg_is_libido:
                                log.info(f"Libido not satisfied (non-sexual msg, drift continues)")
                            else:
                                dg.satisfy_after_response(desire_st, _desire_key, source="对话")
                                log.info(f"Desire satisfied: {_desire_key}")
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
                        # Explicit switch — opens the Route Guard grace window
                        current_session.model_set_at = time_mod.time()
                        current_session._drift_count = 0
                        save_session_meta(current_session)
                    _save_last_model(model)
                    if await tmux_is_running():
                        async with _tmux_send_lock:
                            await tmux_switch_model(model)
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
                # Legacy path, kept for cached old frontends. Useless by
                # construction: this loop is blocked in run_claude during a
                # turn, so the event is only read after the reply finished.
                # The real stop is POST /api/chat-stop.
                if current_session:
                    log.info(f"chat:stop via WS (legacy, turn already over) "
                             f"for session {current_session.id}")


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

            elif event == "curiosity:list":
                seeds = dg.load_curiosity_pool() if DESIRE_ENABLED else []
                await ws.send_json({"event": "curiosity:list", "seeds": seeds})

            elif event == "curiosity:delete":
                seed_id = data.get("id", "")
                if DESIRE_ENABLED and seed_id:
                    ok = dg.delete_curiosity_seed(seed_id)
                    log.info(f"Curiosity seed {'deleted' if ok else 'not found'}: {seed_id}")
                seeds = dg.load_curiosity_pool() if DESIRE_ENABLED else []
                await ws.send_json({"event": "curiosity:list", "seeds": seeds})

            # ── Context management ──


            elif event == "cli:status":
                try:
                    status = tmux_get_status()
                    status["tmux_busy"] = _tmux_send_lock.locked()
                    if current_session:
                        status["session_id"] = current_session.id
                        status["model"] = current_session.model
                    await ws.send_json({"event": "cli:status", **status})
                except Exception as e:
                    await ws.send_json({"event": "cli:error", "message": str(e)})

            elif event == "cli:reconnect":
                try:
                    model = current_session.model if current_session else _load_last_model()
                    await tmux_stop()
                    await tmux_start(model=model)
                    await ws.send_json({"event": "system:error",
                                        "message": "tmux restarting..."})
                    status = tmux_get_status()
                    status["tmux_busy"] = _tmux_send_lock.locked()
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

            elif event == "call:accept":
                call_sid = data.get("sessionId") or (current_session.id if current_session else None)
                call_session = sessions.get(call_sid) if call_sid else current_session
                if call_session:
                    current_session = call_session
                    call_session._in_call = True
                    call_session._call_stop.clear()
                    call_session._call_injected = True
                    try:
                        async with httpx.AsyncClient(timeout=4) as _hc:
                            _hr = await _hc.get(GSVI_BASE_URL)
                            call_session._call_tts_backend = "local" if _hr.status_code < 500 else "minimax"
                    except Exception:
                        call_session._call_tts_backend = "minimax"
                    log.info(f"Incoming call accepted: session={call_sid}, tts={call_session._call_tts_backend}")
                    now_str = datetime.now(SGT).strftime("%Y-%m-%d %H:%M")
                    call_inject = (
                        "[voice-call] Jeoi接听了你的来电，你们现在在语音通话中。\n"
                        "直接写你想说的话，网关会自动TTS播放，不需要调erik_speak。\n"
                        "回复简短口语化——像在打电话，不是写消息。\n"
                        "（）里的内容不会TTS，只作为字幕显示。\n"
                        "说非中文时，每句后面紧跟（中文翻译），一句一译：\n"
                        "I miss you.（我想你。）Come home soon.（快点回来。）\n"
                        "不要把翻译攒到最后一起写。\n"
                        "不要用markdown格式。你是打电话过去的人，说你想说的。"
                    )
                    _accept_idx = append_message(call_session.id, "user", "（接听来电）", source="voice_call")
                    cli_message = call_inject + f"\n\n[{now_str} UTC+8 #{_accept_idx + 1}]\n（Jeoi接听了来电）"
                    await run_claude(cli_message, call_session, ws)

            elif event == "call:reject":
                call_sid = data.get("sessionId") or (current_session.id if current_session else None)
                call_session = sessions.get(call_sid) if call_sid else current_session
                reason = data.get("reason", "rejected")
                if call_session:
                    log.info(f"Incoming call {reason}: session={call_sid}")
                    now_str = datetime.now(SGT).strftime("%Y-%m-%d %H:%M")
                    label = "超时未接听" if reason == "timeout" else "拒绝了来电"
                    notify = f"[{now_str} UTC+8]\n[system] Jeoi{label}。"
                    text, _, _ = await run_cc_oneshot(notify, call_session, max_turns=1)
                    if text:
                        action, content = parse_action(text)
                        if action != "none" and content:
                            await push_pebbling_msg("call_reject", content, call_session)

            elif event == "call:end":
                call_sid = data.get("sessionId") or (current_session.id if current_session else None)
                call_session = sessions.get(call_sid) if call_sid else current_session
                if call_session:
                    call_session._in_call = False
                    call_session._call_injected = False
                    call_session._call_ended_notify = True
                    call_dur = data.get("duration", 0)
                    call_utts = data.get("utterances", [])
                    dur_mm = call_dur // 60
                    dur_ss = call_dur % 60
                    dur_str = f"{dur_mm}:{dur_ss:02d}"
                    append_message(
                        call_session.id, "assistant",
                        f"📞 语音通话 · {dur_str}",
                        source="call_record",
                        call_record={"duration": call_dur, "utterances": call_utts},
                    )
                    call_session.preview = f"📞 通话 {dur_str}"
                    call_session.last_active = datetime.now(SGT)
                    save_session_meta(call_session)
                    log.info(f"Call record saved: {dur_str}, {len(call_utts)} utterances")
                    asyncio.create_task(_send_call_followup(call_session))

            else:
                log.info(f"Unhandled event: {event}")

    except WebSocketDisconnect:
        log.info("WS client disconnected (pebbling worker continues)")
        _user_msg_active = False
        if active_ws is ws:
            active_ws = None
            _ws_last_activity = 0.0
    except Exception as e:
        log.exception(f"WS error: {e}")
        _user_msg_active = False
        if active_ws is ws:
            active_ws = None
            _ws_last_activity = 0.0


# ══════════════════════════════════════════════
#  CLAUDE CLI (via tmux)
# ══════════════════════════════════════════════

async def run_claude(message: str, session: Session, ws: WebSocket):
    """Send message to CC CLI via tmux and stream reply via transcript."""
    global _user_msg_active
    _user_msg_active = True
    session.reset_accumulator()
    tailer = TranscriptTailer(ws, session)

    try:
        need_restart = False
        if not await tmux_is_running():
            need_restart = True
        else:
            # Ask the gateway's own record of what tmux is running — NOT the
            # freshest transcript mtime, which any forge/script write can fake.
            current_cc_id = _tmux_cc_id
            target_cc_id = session.cc_session_id
            if target_cc_id and target_cc_id not in ("channel", "tmux"):
                if current_cc_id != target_cc_id:
                    need_restart = True
            elif not target_cc_id and current_cc_id:
                need_restart = True

        if need_restart:
            await ws.send_json({"event": "system:status",
                                "message": "Switching session..."})
            await restart_cc_for_session(session, ws)

        tailer.start(anchor_text=message)
        async with _tmux_send_lock:
            await tmux_send_message(message)

        await tailer.wait_done(timeout=300)
        if session._in_call:
            await tailer.flush_call_tts()
        await asyncio.sleep(1)
        await tailer.stop()

        turn_usage, turn_cost, last_usage = _read_turn_usage(
            tailer._path, tailer._start_offset, since=tailer._started_at)
        if any(turn_usage.values()):
            _accumulate_session_usage(session, turn_usage, turn_cost)

        # Always sync session ID to whatever transcript the tailer ended on
        # (handles context compression creating a new transcript mid-turn)
        real_id = tailer._path.stem if tailer._path else _get_cc_session_id_from_transcript()
        if real_id:
            _set_tmux_cc_id(real_id)
        if real_id and real_id != session.cc_session_id:
            log.info(f"CC session ID updated: {session.cc_session_id} -> {real_id}")
            session.cc_session_id = real_id
            save_session_meta(session)
        elif not session.cc_session_id or session.cc_session_id in ("channel", "tmux"):
            session.cc_session_id = real_id or "tmux"
            save_session_meta(session)
            log.info(f"CC session ID for {session.id}: {session.cc_session_id}")

        # Post-processing: parse voice markers
        voice_messages = []
        if session._current_text:
            voice_pattern = re.compile(r'<!--voice:(.+?)\|(.+?)\|(.+?)-->')
            for m in voice_pattern.finditer(session._current_text):
                voice_messages.append({
                    "audio_url": m.group(1),
                    "duration": float(m.group(2)),
                    "text": m.group(3),
                })
            session._current_text = voice_pattern.sub('', session._current_text).rstrip()

        # Post-processing: parse sticker reactions.
        # Two addressing modes: #N = house number from the time tag (absolute
        # index in this gateway session's history), ^N = nth-from-last Jeoi
        # message (^1 = her latest). Resolution happens below, after the
        # reply is appended — ^N only counts role=user so that's safe.
        erik_reactions = []
        if session._current_text:
            react_pattern = re.compile(r'<!--react:(.+?):([#^])(\d+)-->')
            for m in react_pattern.finditer(session._current_text):
                emoji, kind, n = m.group(1), m.group(2), int(m.group(3))
                erik_reactions.append((kind, n, emoji))
            session._current_text = react_pattern.sub('', session._current_text).rstrip()

        # Post-processing: parse incoming call marker
        _incoming_call = False
        if session._current_text:
            _call_re = re.compile(r'<!--call:incoming-->')
            if _call_re.search(session._current_text):
                _incoming_call = True
                log.info("Incoming call marker detected")
            session._current_text = _call_re.sub('', session._current_text)

        # Post-processing: parse curiosity seeds (search + ask)
        if session._current_text and DESIRE_ENABLED:
            _seed_search_re = re.compile(r'<!--curiosity-seed:(.+?)-->')
            _seed_ask_re = re.compile(r'<!--curiosity-seed-ask:(.+?)-->')
            trail = list((desire_st.trails.get("curiosity", []) if desire_st else [])[-4:])
            for seed_text in _seed_search_re.findall(session._current_text):
                seed = dg.add_curiosity_seed(seed_text, kind="search", trail=trail)
                log.info(f"Curiosity seed (search): {seed_text[:60]}")
                if active_ws:
                    try:
                        await active_ws.send_json({"event": "curiosity:seed_added", "seed": seed})
                    except Exception:
                        pass
            for seed_text in _seed_ask_re.findall(session._current_text):
                seed = dg.add_curiosity_seed(seed_text, kind="ask", trail=trail)
                log.info(f"Curiosity seed (ask): {seed_text[:60]}")
                if active_ws:
                    try:
                        await active_ws.send_json({"event": "curiosity:seed_added", "seed": seed})
                    except Exception:
                        pass
            session._current_text = _seed_search_re.sub('', session._current_text)
            session._current_text = _seed_ask_re.sub('', session._current_text).rstrip()

        # Post-processing: parse scene-done stamps (Erik's own day-log entry).
        # <!--scene-done:性质--> or <!--scene-done:性质@HH:MM--> (backdated).
        # Authoritative "成场" count — satisfy can't tell heavy talk from a
        # quick scene; the stamp can.
        if session._current_text and DESIRE_ENABLED:
            _scene_re = re.compile(r'<!--scene-done:([^@>]+?)(?:@(\d{1,2}:\d{2}))?-->')
            _stamped = False
            for m in _scene_re.finditer(session._current_text):
                _nature, _at = m.group(1).strip(), m.group(2) or ""
                _t = dg.log_scene_done(_nature, at_time=_at)
                _stamped = True
                log.info(f"Scene stamped: {_nature} @ {_t}")
            if _stamped:
                session._current_text = _scene_re.sub('', session._current_text).rstrip()
                if active_ws:
                    try:
                        await active_ws.send_json({"event": "daylog:updated"})
                    except Exception:
                        pass

        _reply_source = "voice_call" if session._in_call else None

        if session._current_text or session._current_thinking:
            append_message(
                session.id, "assistant", session._current_text,
                thinking=session._current_thinking,
                tools=session._current_tools if session._current_tools else None,
                source=_reply_source,
            )
            for kind, n, emoji in erik_reactions:
                if kind == "#":
                    idx = n - 1
                else:
                    idx = resolve_nth_user_from_tail(session.id, n)
                    if idx is None:
                        log.warning(f"Erik reaction ^{n} ran off history head, dropped")
                        continue
                ok = set_reaction(session.id, idx, "erik", emoji)
                if ok:
                    try:
                        await ws.send_json({
                            "event": "reaction:erik",
                            "msgIndex": idx, "emoji": emoji,
                        })
                    except Exception:
                        pass
                    log.info(f"Erik reacted {emoji} on #{idx + 1} (addressed {kind}{n})")

        for vm in voice_messages:
            append_message(session.id, "assistant", "", voice=vm, source=_reply_source)
            try:
                await ws.send_json({"event": "voice", **vm})
            except Exception:
                pass
            log.info(f"Voice message: {vm['duration']}s")

        if session._current_text or session._current_thinking or voice_messages:
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

        ws_ok = False
        if not session._result_sent:
            try:
                payload = _usage_ws_payload(session, turn_usage, turn_cost,
                                            context_usage=last_usage)
                payload["event"] = "message:complete"
                await ws.send_json(payload)
                ws_ok = True
            except Exception:
                pass

        # Incoming call: send WS event after message:complete
        if _incoming_call:
            try:
                await ws.send_json({
                    "event": "call:incoming",
                    "sessionId": session.id,
                })
                await send_web_push("Erik", "来电", url=f"/call.html?session={session.id}&incoming=true")
                log.info(f"Incoming call event sent for session {session.id}")
            except Exception as e:
                log.warning(f"Incoming call event error: {e}")

        # Web Push 始终发（SW 层 isPageFocused 自动抑制）
        # TG 只在 WS 发送失败时发（说明用户不在页面）
        if session._current_text and not _incoming_call:
            preview = session._current_text.replace(chr(10), " ")[:100]
            await send_web_push("Erik", preview, url="/chat.html")
            if not ws_ok:
                await send_telegram(preview)

        _user_msg_active = False

    except Exception as e:
        _user_msg_active = False
        try:
            await tailer.stop()
        except Exception:
            pass
        log.warning(f"run_claude error: {type(e).__name__}: {e}")
        if session._current_text:
            voice_pat = re.compile(r'<!--voice:(.+?)\|(.+?)\|(.+?)-->')
            cleaned = voice_pat.sub('', session._current_text).rstrip()
            react_pat = re.compile(r'<!--react:(.+?):([#^])(\d+)-->')
            cleaned = react_pat.sub('', cleaned).rstrip()
            _err_source = "voice_call" if session._in_call else None
            append_message(
                session.id, "assistant", cleaned or session._current_text,
                thinking=session._current_thinking,
                tools=session._current_tools if session._current_tools else None,
                source=_err_source,
            )
        ws_err_ok = False
        try:
            await ws.send_json({"event": "system:error", "message": str(e)})
            ws_err_ok = True
        except Exception:
            pass
        if session._current_text and not ws_err_ok:
            preview = session._current_text.replace(chr(10), " ")[:100]
            await send_web_push("Erik", preview, url="/chat.html")
            await send_telegram(preview)


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


@app.post("/api/stardew/event")
async def record_stardew_event(request: Request):
    """Stardew MCP server (Windows) pushes new game events here.
    Body: {"events": [{id, type, companion, ...}, ...]}"""
    global _stardew_last_event_ts
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    events = body.get("events")
    if not isinstance(events, list) or not events:
        return JSONResponse({"error": "events required"}, status_code=400)
    now = time_mod.time()
    for e in events:
        if isinstance(e, dict):
            e["_received"] = now
            _stardew_pending.append(e)
    _stardew_last_event_ts = now
    log.info(f"Stardew events received: {len(events)} (pending={len(_stardew_pending)})")
    return {"ok": True, "count": len(events)}


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


@app.get("/api/daylog")
async def get_daylog():
    """Day log（当日情绪痕迹）for the frontend panel."""
    try:
        return dg._load_day_log()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/daylog")
async def save_daylog(request: Request):
    """Frontend panel edit-save. Body: {date, entries: [...]}. Whole-file replace."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    entries = body.get("entries")
    if not isinstance(entries, list):
        return JSONResponse({"error": "entries must be a list"}, status_code=400)
    cleaned = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        cleaned.append({
            "time": str(e.get("time", ""))[:5],
            "drive": str(e.get("drive", ""))[:16],
            "kind": str(e.get("kind", ""))[:10],
            "peak": round(float(e.get("peak") or 0), 2),
            "note": str(e.get("note", ""))[:40],
            "src": str(e.get("src", ""))[:12],
        })
    cleaned.sort(key=lambda e: dg._mins_since_rollover(e.get("time", "00:00")))
    dg._save_day_log({"date": body.get("date") or dg._day_key(), "entries": cleaned[-60:]})
    log.info(f"Day log saved from frontend: {len(cleaned)} entries")
    return {"ok": True, "count": len(cleaned)}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "sessions": len(sessions),
        "time": datetime.now(SGT).isoformat(),
        "tmux": tmux_get_status(),
        "tmux_busy": _tmux_send_lock.locked(),
    }


# no-cache = 可以缓存但每次必须回源校验（没变就 304，极便宜）。不设这个
# 头，iOS PWA 走启发式缓存，改完前端要靠杀进程/删图标才能拿到新版。
_HTML_NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/chat")
@app.get("/chat.html")
async def serve_chat():
    return FileResponse(Path(CC_CWD) / "chat.html", media_type="text/html",
                        headers=_HTML_NO_CACHE)


@app.get("/")
async def serve_index():
    index = Path(CC_CWD) / "index.html"
    if index.exists():
        return FileResponse(index, media_type="text/html",
                            headers=_HTML_NO_CACHE)
    return FileResponse(Path(CC_CWD) / "chat.html", media_type="text/html",
                        headers=_HTML_NO_CACHE)


if __name__ == "__main__":
    import uvicorn

    os.makedirs("/opt/G-memory-mcp/logs", exist_ok=True)
    uvicorn.run(app, host="0.0.0.0", port=3000)
