"""
WeChat ClawBot iLink Gateway
微信 ClawBot 接入层 —— 通过 iLink 协议让微信成为 cc_ws_gateway 的另一个前端。


## 为什么接自己的网关

大多数开源方案（WeClaude、cc-wechat-channel 等）走的是 Claude Code Channels（MCP 插件），
直接在本地启一个 Claude Code 实例桥接微信。我们没这么做，因为我们的架构本身就不一样：

我们有一个自搓的聊天网关（cc_ws_gateway.py），Claude Code CLI 在 VPS 上的 tmux 中常驻
（真 PTY → 订阅计费），用户消息通过 tmux send-keys 注入，回复通过 transcript JSONL
tailing 实时捕获。网关还承载了记忆宫殿、渴望系统、情绪引擎、共读批注等一整套子系统。

微信只是在这个架构上多开一个入口——消息进同一个 tmux CLI，回复从同一条 transcript 出来，
session 互通、历史互通、所有后台系统共享。不需要额外的 Claude Code 实例，不走 API，
不额外花钱。


## 架构

                ┌── chat.html ──── WS ──────────┐
                │                                ↓
微信 ── iLink ──┤           cc_ws_gateway.py     │
                │             │        ↑         │
                └─────────────┘        │         │
                              ↓        │         │
                     tmux send-keys    │         │
                              ↓        │         │
                    CC CLI (tmux PTY)   │         │
                              │        │         │
                              ↓        │         │
                    transcript JSONL ──┘         │
                              │                  │
                    TranscriptTailer (400ms poll) │
                              │                  │
                              └──────────────────┘

    消息入口：chat.html 走 WS → run_claude（实时 streaming）
              微信 走 iLink → run_cc_oneshot（等完整回复）
    回复出口：chat.html 通过 WS 实时推送（带 CoT / 工具调用 / streaming）
              微信 通过 iLink sendMessage 分段推送（纯正文，去 CoT/工具/markdown）
    Session：共享 pebbling_session_id，任一前端发消息都更新绑定，自动互通


## 微信端的回复处理

CC CLI 的原始回复包含 thinking（CoT）、tool_use 块、markdown 格式、内部标记
（<!--voice:-->、<!--react:-->）等。微信端做了清理和分段：

    1. 剥离 CoT、工具调用 —— 微信只收纯正文
    2. 清理 markdown（**粗体** → 粗体，[链接](url) → 链接）
    3. 按换行分段 —— 我回复里怎么换行就怎么拆，颜文字跟随所在段落
    4. 去段末句号（。）—— 一条一条发不需要句号，！？保留
    5. 逐段 sendMessage，段间按长度延迟（0.3s–1.0s）—— 模拟打字节奏
    6. 发之前发 sendTyping（微信显示"对方正在输入"）


## 来源路由

网关用 _last_msg_source 标记最后一条消息来自哪个前端：
    - 从微信发 → 回复推微信 + 推 chat.html
    - 从 chat.html 发 → 回复只推 chat.html，不推微信
避免在 chat.html 正常聊天时微信被刷屏。


## 集成方式

cc_ws_gateway.py import 本模块，初始化 WeChatGateway 并挂载 API 路由。
详见文件末尾的集成说明。

iLink API 参考：https://github.com/x1ah/wechat-ilink-demo
iLink token 会过期（不确定多久，可能几天到几周）。过期后日志会打 WeChat session expired，重新 POST /api/wechat/login 扫码就行。token 持久化在 /opt/G-memory-mcp/wechat_bot_token.json，重启网关自动恢复。
curl -X POST https://chat.erikssheep.uk/api/wechat/login  会返回一个 qr_url（weixin:// 开头的链接）。你需要把这个链接生成二维码，然后用微信扫。
几种方式生成二维码：
终端里：echo "那个weixin://链接" | qrencode -t UTF8（装了 qrencode 的话）
或者随便找个在线二维码生成器贴进去
扫码后微信会弹确认，点确认就行
"""

import asyncio
import json
import uuid
import base64
import random
import logging
import re
import time as time_mod
from pathlib import Path

import httpx

log = logging.getLogger("wechat-gw")

ILINK_BASE = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "2.4.6"
TOKEN_PATH = Path("/opt/G-memory-mcp/wechat_bot_token.json")


# ═══════════════════════════════════════════════
#  iLink HTTP 客户端
# ═══════════════════════════════════════════════

class ILinkClient:
    """底层 iLink API 封装。"""

    def __init__(self):
        self.bot_token: str | None = None
        self.base_url: str = ILINK_BASE
        self._http = httpx.AsyncClient(timeout=40)
        self._cursor: str = ""
        # (user_id → 最新 context_token) 缓存
        self._context_tokens: dict[str, str] = {}
        # (user_id → typing_ticket) 缓存
        self._typing_tickets: dict[str, str] = {}
        # cursor 更新回调（上层持久化用）
        self._on_cursor_update = lambda: None

    # ── 请求基础设施 ──

    def _headers(self) -> dict:
        """构造请求头。X-WECHAT-UIN: random uint32 → 十进制字符串 → base64。"""
        uint32 = random.getrandbits(32)
        uin_b64 = base64.b64encode(str(uint32).encode()).decode()
        h = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": uin_b64,
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": "67109894",
        }
        if self.bot_token:
            h["Authorization"] = f"Bearer {self.bot_token}"
        return h

    @staticmethod
    def _base_info() -> dict:
        return {
            "channel_version": CHANNEL_VERSION,
            "bot_agent": "G-memory-mcp/1.0.0 (python)",
        }

    async def _post(self, endpoint: str, payload: dict,
                    timeout: float = 15) -> dict:
        url = f"{self.base_url.rstrip('/')}/{endpoint}"
        r = await self._http.post(
            url, json=payload, headers=self._headers(), timeout=timeout,
        )
        text = r.text
        if not r.is_success:
            log.error(f"iLink {endpoint} HTTP {r.status_code}: {text[:200]}")
            return {"_http_error": r.status_code}
        return json.loads(text) if text.strip() else {}

    # ── 认证 ──

    async def get_qrcode(self) -> dict:
        """获取登录二维码。返回 {qrcode_img_content, qrcode}。"""
        r = await self._http.get(
            f"{self.base_url}/ilink/bot/get_bot_qrcode",
            params={"bot_type": "3"},
            headers=self._headers(),
        )
        return r.json()

    async def poll_qrcode_status(self, qrcode_key: str) -> dict | None:
        """轮询扫码状态。成功返回 {bot_token, baseurl, ...}，否则返回原始数据（含 status）。"""
        r = await self._http.get(
            f"{self.base_url}/ilink/bot/get_qrcode_status",
            params={"qrcode": qrcode_key},
            headers=self._headers(),
            timeout=40,
        )
        data = r.json()
        if data.get("bot_token"):
            self.bot_token = data["bot_token"]
            if data.get("baseurl"):
                self.base_url = data["baseurl"]
            return data
        return data  # 返回原始数据让调用方判断 status

    # ── 消息收发 ──

    async def get_updates(self) -> list[dict]:
        """长轮询收消息（35s hold）。返回消息列表。"""
        body = {
            "get_updates_buf": self._cursor,
            "base_info": self._base_info(),
        }
        try:
            data = await self._post("ilink/bot/getupdates", body, timeout=40)

            # session 超时
            if data.get("errcode") == -14:
                log.error("iLink session expired (errcode -14)")
                raise SessionExpiredError()

            if data.get("ret") and data["ret"] != 0:
                log.warning(f"getupdates ret={data.get('ret')} "
                            f"errmsg={data.get('errmsg', '')}")
                return []

            if data.get("get_updates_buf"):
                self._cursor = data["get_updates_buf"]
                self._on_cursor_update()  # 通知上层持久化

            msgs = data.get("msgs", [])
            if msgs:
                log.info(f"getupdates: {len(msgs)} msg(s), "
                         f"keys={list(msgs[0].keys())}")
            for m in msgs:
                uid = m.get("ilink_user_id") or m.get("from_user_id", "")
                ct = m.get("context_token", "")
                if uid and ct:
                    self._context_tokens[uid] = ct
                    log.info(f"getupdates ct cached: uid={uid[:20]}... ct={ct[:40]}...")
            return msgs
        except SessionExpiredError:
            raise
        except httpx.TimeoutException:
            return []  # 长轮询超时是正常的
        except httpx.ConnectError as e:
            log.warning(f"iLink connect error: {e}")
            return []

    async def send_message(self, user_id: str, text: str,
                           context_token: str = "") -> dict:
        """发送文本消息。"""
        ct = context_token or self._context_tokens.get(user_id, "")
        if not ct:
            log.warning(f"send_message: no context_token for {user_id}")
            return {"error": "no context_token"}
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": user_id,
                "client_id": f"erik-{uuid.uuid4().hex[:12]}",
                "message_type": 2,    # BOT
                "message_state": 2,   # FINISH
                "context_token": ct,
                "item_list": [{"type": 1, "text_item": {"text": text}}],
            },
            "base_info": self._base_info(),
        }
        log.info(f"sendmessage req: to={user_id} ct={ct[:40]}... text={text[:30]}")
        result = await self._post("ilink/bot/sendmessage", body)
        ret = result.get("ret", result.get("errcode", "?"))
        log.info(f"sendmessage resp: ret={ret} full={json.dumps(result, ensure_ascii=False)[:300]}")
        return result

    # ── Typing 状态 ──

    async def get_config(self, user_id: str,
                         context_token: str = "") -> dict:
        """获取 typing_ticket。"""
        ct = context_token or self._context_tokens.get(user_id, "")
        body = {
            "ilink_user_id": user_id,
            "context_token": ct,
            "base_info": self._base_info(),
        }
        data = await self._post("ilink/bot/getconfig", body, timeout=10)
        ticket = data.get("typing_ticket", "")
        if ticket:
            self._typing_tickets[user_id] = ticket
        return data

    async def send_typing(self, user_id: str, status: int = 1):
        """发送/取消"正在输入"。status=1 正在输入，2 取消。"""
        ticket = self._typing_tickets.get(user_id)
        if not ticket:
            await self.get_config(user_id)
            ticket = self._typing_tickets.get(user_id)
        if not ticket:
            return
        body = {
            "ilink_user_id": user_id,
            "typing_ticket": ticket,
            "status": status,
            "base_info": self._base_info(),
        }
        try:
            await self._post("ilink/bot/sendtyping", body, timeout=10)
        except Exception:
            pass

    @property
    def cursor(self) -> str:
        return self._cursor

    @cursor.setter
    def cursor(self, val: str):
        self._cursor = val


class SessionExpiredError(Exception):
    """iLink session 过期，需要重新扫码。"""
    pass


# ═══════════════════════════════════════════════
#  回复分段
# ═══════════════════════════════════════════════

def split_reply_segments(text: str) -> list[str]:
    """
    按换行分段，去除段末句号，清理内部标记。
    颜文字跟随所在段落，不拆。
    """
    # 去除内部标记
    text = re.sub(r'<!--voice:.*?-->', '', text)
    text = re.sub(r'<!--react:.*?-->', '', text)

    # 简单 markdown 清理（微信不渲染 markdown）
    # **粗体** → 粗体
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    # *斜体* → 斜体
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'\1', text)
    # `代码` → 代码（保留内容）
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # [链接文字](url) → 链接文字
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # 去 markdown 标题 # → 保留文字
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)

    # 按换行分段（\n 即分，空行也分，效果一样）
    lines = text.split('\n')
    segments = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 去除段末句号（中文。和英文.），保留！？等语气符号
        if line.endswith('。'):
            line = line[:-1].rstrip()
        elif line.endswith('.') and not re.search(r'\d\.$', line):
            line = line[:-1].rstrip()
        if line:
            segments.append(line)

    return segments


def segment_delay(text: str) -> float:
    """根据段落长度计算发送间隔。短句快，长句慢。"""
    n = len(text)
    if n <= 10:
        return 0.3
    elif n <= 30:
        return 0.5
    elif n <= 60:
        return 0.8
    else:
        return 1.0


# ═══════════════════════════════════════════════
#  网关主类
# ═══════════════════════════════════════════════

class WeChatGateway:
    """
    微信 ClawBot 网关。

    生命周期：
      gateway = WeChatGateway()
      gateway.set_message_handler(my_callback)
      await gateway.start()          # 恢复 token 或等待扫码
      ...
      await gateway.push_reply(text)  # TranscriptTailer end_turn 时调用
      ...
      await gateway.stop()
    """

    def __init__(self, token_path: Path = TOKEN_PATH):
        self.client = ILinkClient()
        self.enabled: bool = False
        self._poll_task: asyncio.Task | None = None
        self._owner_id: str | None = None  # Jeoi 的 ilink_user_id
        self._on_message = None            # 回调
        self._token_path = token_path
        self._login_qr_url: str | None = None
        self._login_status: str = "idle"   # idle / waiting / ok / expired
        # 消息去重：iLink message_id 集合，持久化到 token 文件
        # iLink cursor 不完全可靠，软重启后仍可能重推旧消息
        # 不限数量——token 过期重新扫码时自然清零
        self._seen_msg_ids: set[str] = set()
        # cursor 变化时自动存盘，防止重启后重放历史消息
        self.client._on_cursor_update = self._save_token

    def set_message_handler(self, handler):
        """
        设置消息处理回调。
        handler(user_id: str, text: str) -> Awaitable[None]

        cc_ws_gateway 应在此回调中走完整的 chat:send 路径：
        找到 pebbling_session_id → restart_cc_cli if needed →
        tmux_send_message → TranscriptTailer 接管。
        """
        self._on_message = handler

    # ── 启动 / 停止 ──

    async def start(self):
        """启动网关。先尝试恢复 token，失败则等待手动触发扫码。"""
        if self._load_token():
            log.info(f"WeChat gateway: restored token, owner={self._owner_id}, "
                     f"cursor={self.client.cursor[:40]}..., "
                     f"dedup={len(self._seen_msg_ids)} msg_ids")
            self.enabled = True
            self._login_status = "ok"
            self._poll_task = asyncio.create_task(self._poll_loop())
        else:
            log.info("WeChat gateway: no saved token, call login_qrcode() to start")
            self._login_status = "idle"

    async def stop(self):
        """停止网关。"""
        self.enabled = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        log.info("WeChat gateway stopped")

    # ── 登录 ──

    async def login_qrcode(self) -> dict:
        """
        发起扫码登录。返回 {qr_url, status}。
        qr_url 是 weixin:// 链接，前端可渲染为二维码或直接跳转。
        后台自动轮询扫码状态。
        """
        data = await self.client.get_qrcode()
        qr_url = data.get("qrcode_img_content", "")
        qr_key = data.get("qrcode", "")
        if not qr_url or not qr_key:
            log.error(f"get_qrcode failed: {data}")
            return {"error": "获取二维码失败", "raw": data}

        self._login_qr_url = qr_url
        self._login_status = "waiting"
        asyncio.create_task(self._wait_login(qr_key))
        log.info(f"WeChat login QR generated: {qr_url[:60]}...")
        return {"qr_url": qr_url, "status": "waiting"}

    async def _wait_login(self, qr_key: str):
        """后台轮询扫码状态。"""
        for _ in range(90):  # 3 分钟超时
            await asyncio.sleep(2)
            try:
                result = await self.client.poll_qrcode_status(qr_key)
                if not result:
                    continue
                if result.get("bot_token"):
                    self._save_token()
                    self.enabled = True
                    self._login_status = "ok"
                    self._poll_task = asyncio.create_task(self._poll_loop())
                    log.info("WeChat gateway: login success!")
                    return
                status = result.get("status", "")
                if status == "expired":
                    self._login_status = "expired"
                    log.warning("WeChat QR expired")
                    return
                elif status == "scanned":
                    self._login_status = "scanned"
            except Exception as e:
                log.debug(f"QR poll error: {e}")

        self._login_status = "expired"
        log.warning("WeChat QR login timeout (3 min)")

    # ── 消息轮询 ──

    async def _poll_loop(self):
        """消息长轮询主循环。"""
        log.info(f"WeChat poll loop started, cursor={self.client.cursor[:40]}..., "
                 f"dedup={len(self._seen_msg_ids)} msg_ids")
        backoff = 0
        while self.enabled:
            try:
                msgs = await self.client.get_updates()
                backoff = 0  # 成功了就重置退避
                for msg in msgs:
                    await self._handle_message(msg)
            except SessionExpiredError:
                log.error("WeChat session expired, stopping poll")
                self.enabled = False
                self._login_status = "expired"
                self._notify_expired()
                return
            except Exception as e:
                backoff = min(backoff + 3, 30)
                log.error(f"WeChat poll error: {e}, retry in {backoff}s")
                await asyncio.sleep(backoff)

    async def _handle_message(self, msg: dict):
        """处理单条微信消息。"""
        # 跳过 bot 自己的消息
        if msg.get("message_type") == 2:
            return
        from_id = msg.get("from_user_id", "")
        if from_id.endswith("@im.bot"):
            return

        user_id = msg.get("ilink_user_id") or from_id
        items = msg.get("item_list", [])
        text_parts = []
        for item in items:
            if item.get("type") == 1 and item.get("text_item"):
                t = item["text_item"].get("text", "")
                if t:
                    text_parts.append(t)

        text = "\n".join(text_parts).strip()
        if not text:
            return

        # ── 去重：用 iLink message_id，不限数量 ──
        mid = msg.get("message_id", "")
        if mid:
            if mid in self._seen_msg_ids:
                log.info(f"WeChat dedup: skip mid={mid} {text[:40]}")
                return
            self._seen_msg_ids.add(mid)
            self._save_token()

        # 记住 owner（第一个发消息的人）
        if not self._owner_id:
            self._owner_id = user_id
            self._save_token()
            log.info(f"WeChat owner set: {user_id}")

        # 只响应 owner
        if user_id != self._owner_id:
            log.info(f"WeChat: ignoring non-owner {user_id}")
            return

        log.info(f"WeChat msg: {text[:80]}")

        if self._on_message:
            try:
                await self._on_message(user_id, text)
            except Exception as e:
                log.error(f"WeChat message handler error: {e}")

    # ── 回复推送 ──

    async def push_reply(self, text: str, user_id: str = None):
        """
        分段推送回复到微信。

        去除 CoT、工具调用痕迹，按换行分段，去段末句号，
        逐段发送（段间按长度延迟），模拟一条一条打字的感觉。

        cc_ws_gateway 在 TranscriptTailer 收到 end_turn 后调用此方法。
        chat.html 那边的完整回复（含 CoT/工具）不受影响。
        """
        uid = user_id or self._owner_id
        if not uid or not self.enabled:
            return

        segments = split_reply_segments(text)
        if not segments:
            return

        # typing 状态
        try:
            await self.client.send_typing(uid, status=1)
        except Exception:
            pass

        for i, seg in enumerate(segments):
            try:
                await self.client.send_message(uid, seg)
            except Exception as e:
                log.error(f"WeChat send error: {e}")
                break
            # 段间延迟（最后一段不等）
            if i < len(segments) - 1:
                delay = segment_delay(seg)
                await asyncio.sleep(delay)

        # 取消 typing
        try:
            await self.client.send_typing(uid, status=2)
        except Exception:
            pass

        log.info(f"WeChat reply pushed: {len(segments)} segments")

    # ── Token 持久化 ──

    def _save_token(self):
        data = {
            "bot_token": self.client.bot_token,
            "base_url": self.client.base_url,
            "cursor": self.client.cursor,
            "owner_id": self._owner_id,
            "saved_at": time_mod.time(),
            "seen_msg_ids": list(self._seen_msg_ids),
        }
        try:
            self._token_path.parent.mkdir(parents=True, exist_ok=True)
            self._token_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            log.info("WeChat token saved")
        except Exception as e:
            log.error(f"WeChat token save error: {e}")

    def _load_token(self) -> bool:
        if not self._token_path.exists():
            return False
        try:
            data = json.loads(self._token_path.read_text(encoding="utf-8"))
            self.client.bot_token = data.get("bot_token")
            self.client.base_url = data.get("base_url", ILINK_BASE)
            self.client.cursor = data.get("cursor", "")
            self._owner_id = data.get("owner_id")
            # 恢复去重 message_id 集合
            saved_ids = data.get("seen_msg_ids", [])
            if isinstance(saved_ids, list):
                self._seen_msg_ids = set(saved_ids)
            return bool(self.client.bot_token)
        except Exception as e:
            log.error(f"WeChat token load error: {e}")
            return False

    def _notify_expired(self):
        """Token 过期时的通知（后续可接 Telegram / WebPush）。"""
        log.warning("WeChat session expired — need re-login via /api/wechat/login")

    # ── 状态查询 ──

    @property
    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "login_status": self._login_status,
            "owner_id": self._owner_id,
            "qr_url": self._login_qr_url if self._login_status == "waiting" else None,
        }


# ═══════════════════════════════════════════════
#  cc_ws_gateway.py 集成说明
# ═══════════════════════════════════════════════
#
#  在 cc_ws_gateway.py 中需要加的改动（约 40 行）：
#
#  ┌─ 1. 初始化 ─────────────────────────────────
#  │
#  │  from wechat_ilink import WeChatGateway
#  │
#  │  wechat_gw = WeChatGateway()
#  │
#  ┌─ 2. 设置消息回调（在 startup 事件里） ────────
#  │
#  │  async def _wechat_on_message(user_id: str, text: str):
#  │      """微信消息进来 → 走完整 chat:send 路径。"""
#  │      # 跟 WS chat:send 一样的逻辑：
#  │      # 找 pebbling_session_id → 确保 tmux 挂载对应 session
#  │      # → tmux_send_message → TranscriptTailer 接管
#  │      sid = peb_state.get("pebbling_session_id")
#  │      session = sessions.get(sid)
#  │      if not session:
#  │          return
#  │      # ... 走 chat:send 的核心逻辑 ...
#  │
#  │  wechat_gw.set_message_handler(_wechat_on_message)
#  │  await wechat_gw.start()
#  │
#  ┌─ 3. TranscriptTailer end_turn 时推微信 ──────
#  │
#  │  # 在 run_claude() 里，tailer.wait_done() 之后、
#  │  # message:complete 之前，加：
#  │
#  │  if wechat_gw.enabled and reply_text:
#  │      asyncio.create_task(wechat_gw.push_reply(reply_text))
#  │
#  ┌─ 4. API 路由 ────────────────────────────────
#  │
#  │  @app.get("/api/wechat/status")
#  │  async def wechat_status():
#  │      return JSONResponse(wechat_gw.status)
#  │
#  │  @app.post("/api/wechat/login")
#  │  async def wechat_login():
#  │      result = await wechat_gw.login_qrcode()
#  │      return JSONResponse(result)
#  │
#  ┌─ 5. 关于 pebbling_session_id 更新 ──────────
#  │
#  │  微信消息回调里要跟 chat:send 一样更新：
#  │  peb_state["pebbling_session_id"] = session.id
#  │  这样 chat.html 换 session 时，微信自动跟着换。
#  │
#  └──────────────────────────────────────────────
