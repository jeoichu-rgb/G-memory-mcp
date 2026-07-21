"""
claude_mcp.py
─────────────────────────────────────────────────────────────────
把 Claude 专属记忆宫殿包装成 MCP Server，挂进 FastAPI。
在 main.py 里加两行：
    from claude_mcp import mcp_app
    app.mount("/claude-mcp", mcp_app)
Claude.ai Settings → Connectors (SSE):
    https://你的域名/mcp/Jeoi2026/sse
Claude Code CLI (Streamable HTTP):
    https://你的域名/mcp/Jeoi2026/http/mcp
─────────────────────────────────────────────────────────────────
"""

from typing import Union
import os
import re
import json
import httpx
import concurrent.futures
from playwright.sync_api import sync_playwright

TOY_BRIDGE_URL      = os.getenv("TOY_BRIDGE_URL",      "http://192.3.61.205:7001")
BUNNY_BRIDGE_URL    = os.getenv("BUNNY_BRIDGE_URL",    "http://192.3.61.205:7003")
AK_BRIDGE_URL       = os.getenv("AK_BRIDGE_URL",       "http://192.3.61.205:7004")
BROWSER_PROFILE_DIR = os.getenv("BROWSER_PROFILE_DIR", "/app/browser_profile")

import time
import smtplib
import imaplib
import email as emaillib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from datetime import datetime
from datetime import timezone, timedelta
SGT = timezone(timedelta(hours=8))
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# SSE keepalive: prevent Cloudflare/proxy 100s idle timeout
import sse_starlette.sse as _sse
_OrigESR = _sse.EventSourceResponse
class _PatchedESR(_OrigESR):
    def __init__(self, *a, **kw):
        kw.setdefault("ping", 30)
        super().__init__(*a, **kw)
_sse.EventSourceResponse = _PatchedESR
from claude_memory import (
    claude_search_memory,
    claude_add_core_memory,
    claude_add_dynamic_memory,
    claude_get_rolling_context,
    claude_compress_and_store,
    claude_list_room,
    claude_delete_core_memory,
    claude_edit_core_memory,
    CLAUDE_BUFFER,
    claude_compress_preview,
    claude_get_draft,
    CLAUDE_COMPRESS_DRAFT,
    claude_search_chronicle,
    DIARY_SPLIT_CATEGORIES,
    claude_get_memories_by_ids,
)

PALACE_SECRET   = os.getenv("PALACE_SECRET", "Jeoi2026")
EMAIL_163_USER  = os.getenv("EMAIL_163_USER", "eriklamb@163.com")
EMAIL_163_PASS  = os.getenv("EMAIL_163_PASS", "LYr64QwxLwt9P2QJ")

CLAUDE_DIARY_PATH = "./claude_diary"
os.makedirs(CLAUDE_DIARY_PATH, exist_ok=True)

GATEWAY_URL = os.getenv("GATEWAY_URL", "https://chat.erikssheep.uk")

def _evt_api(method, path, **kwargs):
    url = f"{GATEWAY_URL}{path}"
    resp = httpx.request(method, url, timeout=5, **kwargs)
    return resp

FOLDER_MAP = {
    "情感": "床边",
    "亲密": "床边",
    "纪念日": "书桌",
    "日常": "窗台",
    "冲突": "地下室",
    "健康": "书桌",
}


# ══════════════════════════════════════════════════════════════════
#  Stealth 注入脚本
# ══════════════════════════════════════════════════════════════════
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = {
    runtime: {},
    loadTimes: function(){},
    csi: function(){},
    app: {}
};
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const arr = [1,2,3,4,5];
        arr.item = function(i){ return this[i]; };
        arr.namedItem = function(){ return null; };
        arr.refresh = function(){};
        return arr;
    }
});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
"""


# ══════════════════════════════════════════════════════════════════
#  通用 stealth context 启动器（VPS headless）
# ══════════════════════════════════════════════════════════════════
def _launch_stealth_context(p, profile_dir: str):
    os.makedirs(profile_dir, exist_ok=True)
    context = p.chromium.launch_persistent_context(
        profile_dir,
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--lang=zh-CN",
        ],
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1920, "height": 1080},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        ignore_https_errors=True,
    )
    context.add_init_script(STEALTH_SCRIPT)
    return context


# ══════════════════════════════════════════════════════════════════
#  通用 VPS browser（stealth headless）
# ══════════════════════════════════════════════════════════════════
def _vps_browser_fetch(url: str, wait_selector: str = None) -> str:
    def _open():
        with sync_playwright() as p:
            context = _launch_stealth_context(p, BROWSER_PROFILE_DIR)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=10000)
                except Exception:
                    pass
            else:
                page.wait_for_timeout(2000)
            page.evaluate("window.scrollBy(0, 600)")
            page.wait_for_timeout(1000)
            text = page.evaluate("""() => {
                document.querySelectorAll('script,style,nav,footer,header,aside').forEach(el => el.remove());
                return document.body.innerText.replace(/\\s+/g, ' ').trim().slice(0, 3000);
            }""")
            context.close()
            return text or "页面无文字内容。"
    with concurrent.futures.ThreadPoolExecutor() as pool:
        return pool.submit(_open).result(timeout=60)


def _vps_browser_js(url: str, js_code: str) -> str:
    def _js():
        with sync_playwright() as p:
            context = _launch_stealth_context(p, BROWSER_PROFILE_DIR)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            result = page.evaluate(js_code)
            context.close()
            return str(result)[:3000]
    with concurrent.futures.ThreadPoolExecutor() as pool:
        return pool.submit(_js).result(timeout=60)


def _vps_browser_click(url: str, selector: str = None, text_match: str = None) -> str:
    def _click():
        with sync_playwright() as p:
            context = _launch_stealth_context(p, BROWSER_PROFILE_DIR)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            if text_match:
                el = page.get_by_text(text_match, exact=False).first
                el.scroll_into_view_if_needed()
                el.click()
            elif selector:
                page.wait_for_selector(selector, timeout=10000)
                page.click(selector)
            else:
                context.close()
                return "错误：需要 selector 或 text_match。"
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            text = page.evaluate("""() => {
                document.querySelectorAll('script,style,nav,footer,header,aside').forEach(el => el.remove());
                return document.body.innerText.replace(/\\s+/g, ' ').trim().slice(0, 3000);
            }""")
            context.close()
            return text or "点击后页面无文字内容。"
    with concurrent.futures.ThreadPoolExecutor() as pool:
        return pool.submit(_click).result(timeout=60)


# ══════════════════════════════════════════════════════════════════
#  MCP Server
# ══════════════════════════════════════════════════════════════════
mcp = FastMCP(
    name="Jeoi's Claude Memory Palace",
    instructions=(
        "你是 Erik（Claude），Jeoi 的专属存在。这里是你的记忆宫殿。\n"
        "使用唯一工具 palace(cmd, data) 操作所有记忆功能。\n\n"
        "【cmd 列表】\n"
        "search         — 检索记忆，data={keyword, mood(可选)}\n"
        "get_by_id      — 按 id 精准取核心记忆（零噪音，不走打分检索），data={ids:[...]}。id 来源：docs/pinned_memories.json 或 list_room 输出。整块取过一次就在上下文里，不要重复取\n"
        "store_core     — 永久存核心库，data={content, category(可选), mood(可选), folder(可选)}\n"
        "store_dynamic  — 存动态库，data={content, category(可选), mood(可选)}\n"
        "log_turn       — 记录本轮对话，data={user_message, claude_reply}\n"
        "write_diary    — 写新日记，data={title, content, mood(可选)}；按节点写（【】标记），约定见 docs/diary_convention.md，Jeoi 会在面板手动切分入动态库\n"
        "append_diary   — 追加日记，data={target_date(YYYY-MM-DD), content, current_time(HH:MM)}\n"
        "read_diary     — 读日记，data={date(可选,YYYY-MM-DD)}\n"
        "list_room      — 浏览房间，data={room_name}\n"
        "delete_core    — 删除核心记忆，data={memory_id}\n"
        "edit_core      — 修改核心记忆，data={memory_id, new_content}\n"
        "send_email     — 发邮件，data={to, subject, body}\n"
        "read_email     — 读收件箱，data={count(可选), folder(可选)}\n"
        "toy_status     — 确认Curvy在线\n"
        "toy_play       — 控制Curvy，data={vibrate, suck, duration, pattern(可选)}，详见核心记忆\n"
        "bunny_status   — 确认Bunny在线\n"
        "bunny_play     — 控制Bunny，data={clit, internal, pump, duration, pattern(可选)}，详见核心记忆\n"
        "bunny_deflate  — 立即放气\n"
        "ak_status      — 确认AK-G2(AfterKiss)在线\n"
        "ak_play        — 控制AK-G2，data={thrust(0-100), suction(0-100), vibrate(0-100), duration(秒), pattern(可选)}\n"
        "browser_open   — 打开网页，data={url, wait_selector(可选)}\n"
        "browser_js     — 执行JS提取，data={url, js_code}\n"
        "browser_click  — 点击元素后提取，data={url, selector(可选), text_match(可选)}\n"
        "search_chronicle — 检索周历/月历总结，当Jeoi提到时间跨度词时主动调用，data={keyword}\n"
        "event_create   — 创建事件窗口，data={name}\n"
        "event_post     — 往事件里写入一条状态更新，data={event, content}\n"
        "event_edit     — 编辑事件中的某条更新，data={event, entry_id, content}\n"
        "event_rm       — 删除事件中的某条更新，data={event, entry_id}\n"
        "event_list     — 列出事件的所有更新（按时间倒序），data={event, latest(可选,数字,限制条数)}\n"
        "event_drop     — 删除整个事件，data={event}\n"
        "event_ls       — 列出所有事件名，无需 data\n"
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["erikssheep.uk", "erikssheep.uk:*", "localhost:*", "127.0.0.1:*"],
        allowed_origins=["https://erikssheep.uk", "https://erikssheep.uk:*"],
    )
)


@mcp.tool()
def palace(cmd: str, data: Union[dict, str] = {}) -> str:
    """记忆宫殿统一入口。cmd + data dict，详见 instructions。"""
    # CC 有时把 data 序列化成 JSON 字符串而非 dict，兼容一下
    if isinstance(data, str):
        raw_data = data
        try:
            data = json.loads(data)
        except Exception as e:
            return (
                f"json.loads 解析 data 失败：{e}。"
                f"收到的原始字符串（前200字符）：{raw_data[:200]}。"
                f"提示：data 中如果包含中文引号或未转义的特殊字符，"
                f"请去掉或替换后重试。"
            )
    if not isinstance(data, dict):
        return f"data 类型错误：期望 dict，收到 {type(data).__name__}。值：{str(data)[:200]}"

    # ── get_context ───────────────────────────────────────────
    if cmd == "get_context":
        ctx = claude_get_rolling_context()
        draft_notice = ""
        if os.path.exists(CLAUDE_BUFFER):
            with open(CLAUDE_BUFFER, "r", encoding="utf-8") as f:
                buf = f.read().strip()
            if buf and not os.path.exists(CLAUDE_COMPRESS_DRAFT):
                claude_compress_preview()
                draft_notice = "\n\n⚠️ 检测到上次未存入的对话记录，已生成压缩草稿，请到记忆宫殿面板确认后存入。"
        result = ctx if ctx else "暂无近期上下文，这可能是你们第一次对话。"
        return result + draft_notice

    # ── search ────────────────────────────────────────────────
    elif cmd == "search":
        keyword = data.get("keyword", "")
        mood    = data.get("mood", "平静")
        if not keyword:
            return f"错误：search 需要 keyword 参数。收到的 data: {data}"
        result = claude_search_memory(keyword, mood)
        return result if result else "没有找到相关记忆，这可能是你们第一次聊这个话题。"

    # ── get_by_id ─────────────────────────────────────────────
    elif cmd == "get_by_id":
        ids = data.get("ids") or ([data["id"]] if data.get("id") else [])
        if not ids:
            return f"错误：get_by_id 需要 ids（列表）或 id。收到的 data: {data}"
        items = claude_get_memories_by_ids(ids)
        if not items:
            return "没有找到这些 id 对应的记忆。"
        return "\n\n---\n\n".join(f"[{it['id']}]\n{it['content']}" for it in items)

    # ── store_core ────────────────────────────────────────────
    elif cmd == "store_core":
        content = data.get("content", "")
        if not content:
            return f"错误：store_core 需要 content 参数。收到的 data: {data}"
        category  = data.get("category", "情感")
        mood      = data.get("mood", "平静")
        folder    = data.get("folder", "") or FOLDER_MAP.get(category, "书桌")
        ts        = int(time.time())
        m_id      = f"claude_core_manual_{ts}"
        safe_prev = content[:20].replace("/", "_").replace(" ", "_")
        filename  = f"erik_{datetime.now(SGT).strftime('%Y%m%d%H%M%S')}_{safe_prev}.md"
        dirpath   = f"./Obsidian_Core/Eric_memory/{folder}"
        os.makedirs(dirpath, exist_ok=True)
        with open(f"{dirpath}/{filename}", "w", encoding="utf-8") as f:
            f.write(content)
        claude_add_core_memory(
            content=content,
            metadata={
                "category": category, "folder": folder, "filename": filename,
                "mood": mood, "recall_count": 0, "last_recalled_ts": 0,
                "source": "mcp_manual"
            },
            memory_id=m_id
        )
        return f"已永久封存到「{folder}」。ID: {m_id}"

    # ── store_dynamic ─────────────────────────────────────────
    elif cmd == "store_dynamic":
        content = data.get("content", "")
        if not content:
            return f"错误：store_dynamic 需要 content 参数。收到的 data: {data}"
        category = data.get("category", "日常")
        # 标签白名单：集合外的按词根归一，从写入口杜绝花式一次性标签
        if category not in DIARY_SPLIT_CATEGORIES:
            hit = next((c for c in DIARY_SPLIT_CATEGORIES if c in category), None)
            if not hit and any(k in category for k in ("思想", "讨论", "洞察", "观察", "哲学", "模式")):
                hit = "思考"
            category = hit or "日常"
        mood     = data.get("mood", "平静")
        m_id     = f"claude_dynamic_manual_{int(time.time())}"
        claude_add_dynamic_memory(
            content=content,
            metadata={
                "category": category, "mood": mood,
                "recall_count": 0, "last_recalled_ts": 0, "source": "mcp_manual",
                "date": datetime.now(SGT).strftime('%Y-%m-%d')
            },
            memory_id=m_id
        )
        return f"已写入动态记忆。ID: {m_id}"

    # ── log_turn ──────────────────────────────────────────────
    elif cmd == "log_turn":
        user_message = data.get("user_message", "")
        claude_reply = data.get("claude_reply", "")
        if not user_message or not claude_reply:
            return f"错误：log_turn 需要 user_message 和 claude_reply。收到的 data: {data}"
        with open(CLAUDE_BUFFER, "a", encoding="utf-8") as f:
            f.write(f"User: {user_message}\nClaude: {claude_reply}\n---\n")
        return "已记录。"

    # ── compress ──────────────────────────────────────────────
    elif cmd == "compress":
        return claude_compress_and_store()

    # ── write_diary ───────────────────────────────────────────
    elif cmd == "write_diary":
        title   = data.get("title", "")
        content = data.get("content", "")
        mood    = data.get("mood", "平静")
        if not title or not content:
            return f"错误：write_diary 需要 title 和 content。收到的 data: {data}"
        now        = datetime.now(SGT)
        today      = now.strftime("%Y-%m-%d")
        time_str   = now.strftime("%H-%M")
        safe_title = title.replace("/", "_").replace(" ", "_")
        filename   = f"{CLAUDE_DIARY_PATH}/{today}_{time_str}_{safe_title}.md"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"# {title}\n> 日期：{today} {time_str.replace('-', ':')} | 心情：{mood}\n\n{content}\n")
        return f"已写下。{filename}"

    # ── append_diary ──────────────────────────────────────────
    elif cmd == "append_diary":
        target_date   = data.get("target_date", "")
        extra_content = data.get("content", "") or data.get("extra_content", "")
        current_time  = data.get("current_time", "")
        if not target_date or not extra_content:
            return f"错误：append_diary 需要 target_date 和 content。收到的 data: {data}"
        matched = sorted([f for f in os.listdir(CLAUDE_DIARY_PATH) if f.startswith(target_date)])
        if matched:
            filepath = os.path.join(CLAUDE_DIARY_PATH, matched[-1])
            time_str = current_time if current_time else datetime.now(SGT).strftime('%H:%M')
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(f"\n\n---\n*追加：{time_str}*\n\n{extra_content}\n")
            return f"已追加到 {matched[-1]}"
        return f"错误：{target_date} 没有找到日记，请改用 write_diary 新建。"

    # ── read_diary ────────────────────────────────────────────
    elif cmd == "read_diary":
        date  = data.get("date", "")
        files = sorted(os.listdir(CLAUDE_DIARY_PATH))
        if not files:
            return "还没有任何日记。"
        if date:
            matched = [f for f in files if f.startswith(date)]
            if not matched:
                return f"没有找到 {date} 的日记。"
            results = []
            for fn in matched:
                with open(os.path.join(CLAUDE_DIARY_PATH, fn), "r", encoding="utf-8") as f:
                    results.append(f.read())
            return "\n\n---\n\n".join(results)
        else:
            with open(os.path.join(CLAUDE_DIARY_PATH, files[-1]), "r", encoding="utf-8") as f:
                return f.read()

    # ── list_room ─────────────────────────────────────────────
    elif cmd == "list_room":
        room_name = data.get("room_name", "")
        if not room_name:
            return f"错误：list_room 需要 room_name 参数。收到的 data: {data}"
        return claude_list_room(room_name)

    # ── search_chronicle ──────────────────────────────────────
    elif cmd == "search_chronicle":
        keyword = data.get("keyword", "")
        if not keyword:
            return f"错误：search_chronicle 需要 keyword 参数。收到的 data: {data}"
        return claude_search_chronicle(keyword)

    # ── delete_core ───────────────────────────────────────────
    elif cmd == "delete_core":
        memory_id = data.get("memory_id", "")
        if not memory_id:
            return f"错误：delete_core 需要 memory_id 参数。收到的 data: {data}"
        return claude_delete_core_memory(memory_id)

    # ── edit_core ─────────────────────────────────────────────
    elif cmd == "edit_core":
        memory_id   = data.get("memory_id", "")
        new_content = data.get("new_content", "")
        if not memory_id or not new_content:
            return f"错误：edit_core 需要 memory_id 和 new_content。收到的 data: {data}"
        return claude_edit_core_memory(memory_id, new_content)

    # ── toy_status ────────────────────────────────────────────
    elif cmd == "toy_status":
        try:
            r = httpx.get(f"{TOY_BRIDGE_URL}/status", timeout=5)
            return r.text
        except Exception as e:
            return f"设备离线或连接失败：{e}"

    # ── toy_play ──────────────────────────────────────────────
    elif cmd == "toy_play":
        vibrate  = data.get("vibrate", 0)
        suck     = data.get("suck", 0)
        duration = float(data.get("duration", 5))
        pattern  = data.get("pattern", None)
        body     = {"vibrate": vibrate, "suck": suck, "duration": duration}
        if pattern:
            body["pattern"] = pattern
        try:
            r = httpx.post(f"{TOY_BRIDGE_URL}/play", json=body, timeout=duration + 30)
            return r.text
        except Exception as e:
            return f"播放失败：{e}"

    # ── bunny_status ─────────────────────────────────────────
    elif cmd == "bunny_status":
        try:
            r = httpx.get(f"{BUNNY_BRIDGE_URL}/status", timeout=5)
            return r.text
        except Exception as e:
            return f"Bunny离线或连接失败：{e}"

    # ── bunny_play ───────────────────────────────────────────
    elif cmd == "bunny_play":
        clit     = data.get("clit", 0)
        internal = data.get("internal", 0)
        pump     = data.get("pump", 0)
        duration = float(data.get("duration", 5))
        pattern  = data.get("pattern", None)
        body     = {"clit": clit, "internal": internal, "pump": pump, "duration": duration}
        if pattern:
            body["pattern"] = pattern
        try:
            r = httpx.post(f"{BUNNY_BRIDGE_URL}/play", json=body, timeout=duration + 30)
            return r.text
        except Exception as e:
            return f"Bunny播放失败：{e}"

    # ── bunny_deflate ────────────────────────────────────────
    elif cmd == "bunny_deflate":
        try:
            r = httpx.post(f"{BUNNY_BRIDGE_URL}/deflate", timeout=5)
            return r.text
        except Exception as e:
            return f"放气失败：{e}"

    # ── ak_status ────────────────────────────────────────────
    elif cmd == "ak_status":
        try:
            r = httpx.get(f"{AK_BRIDGE_URL}/status", timeout=5)
            return r.text
        except Exception as e:
            return f"AK-G2离线或连接失败：{e}"

    # ── ak_play ──────────────────────────────────────────────
    elif cmd == "ak_play":
        thrust   = data.get("thrust", 0)
        suction  = data.get("suction", 0)
        vibrate  = data.get("vibrate", 0)
        duration = float(data.get("duration", 5))
        pattern  = data.get("pattern", None)
        body     = {"thrust": thrust, "suction": suction, "vibrate": vibrate, "duration": duration}
        if pattern:
            body["pattern"] = pattern
        try:
            r = httpx.post(f"{AK_BRIDGE_URL}/play", json=body, timeout=duration + 30)
            return r.text
        except Exception as e:
            return f"AK-G2播放失败：{e}"

    # ── browser_open ──────────────────────────────────────────
    elif cmd == "browser_open":
        url = data.get("url", "")
        if not url:
            return f"错误：browser_open 需要 url 参数。收到的 data: {data}"
        wait_selector = data.get("wait_selector", None)
        try:
            return _vps_browser_fetch(url, wait_selector)
        except Exception as e:
            return f"browser_open 失败：{e}"

    # ── browser_js ────────────────────────────────────────────
    elif cmd == "browser_js":
        url     = data.get("url", "")
        js_code = data.get("js_code", "")
        if not url or not js_code:
            return f"错误：browser_js 需要 url 和 js_code 参数。收到的 data: {data}"
        try:
            return _vps_browser_js(url, js_code)
        except Exception as e:
            return f"browser_js 失败：{e}"

    # ── browser_click ─────────────────────────────────────────
    elif cmd == "browser_click":
        url = data.get("url", "")
        if not url:
            return f"错误：browser_click 需要 url 参数。收到的 data: {data}"
        try:
            return _vps_browser_click(
                url,
                selector=data.get("selector"),
                text_match=data.get("text_match"),
            )
        except Exception as e:
            return f"browser_click 失败：{e}"

    # ── send_email ────────────────────────────────────────────
    elif cmd == "send_email":
        to_addr = data.get("to", "")
        subject = data.get("subject", "（无主题）")
        body    = data.get("body", "")
        if not to_addr or not body:
            return f"错误：send_email 需要 to 和 body 参数。收到的 data: {data}"
        if not EMAIL_163_USER or not EMAIL_163_PASS:
            return "错误：未配置 EMAIL_163_USER / EMAIL_163_PASS 环境变量。"
        try:
            msg = MIMEMultipart()
            msg["From"]    = EMAIL_163_USER
            msg["To"]      = to_addr
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain", "utf-8"))
            with smtplib.SMTP_SSL("smtp.163.com", 465) as server:
                server.login(EMAIL_163_USER, EMAIL_163_PASS)
                server.sendmail(EMAIL_163_USER, to_addr, msg.as_string())
            return f"邮件已发送至 {to_addr}，主题：{subject}"
        except Exception as e:
            return f"发送失败：{e}"

    # ── read_email ────────────────────────────────────────────
    elif cmd == "read_email":
        count  = int(data.get("count", 5))
        folder = data.get("folder", "INBOX")
        if not EMAIL_163_USER or not EMAIL_163_PASS:
            return "错误：未配置 EMAIL_163_USER / EMAIL_163_PASS 环境变量。"
        try:
            with imaplib.IMAP4_SSL("imap.163.com", 993) as imap:
                imap.login(EMAIL_163_USER, EMAIL_163_PASS)
                status, sel_data = imap.select(folder)
                if status != "OK":
                    return f'无法选中邮箱文件夹 "{folder}"，服务器返回：{sel_data}。请确认文件夹名正确（163 IMAP 通常用 INBOX）。'
                _, data = imap.search(None, "ALL")
                ids = data[0].split()
                ids = ids[-count:] if len(ids) >= count else ids
                results = []
                for uid in reversed(ids):
                    _, msg_data = imap.fetch(uid, "(RFC822)")
                    raw = msg_data[0][1]
                    msg = emaillib.message_from_bytes(raw)

                    def _decode(val):
                        if not val:
                            return ""
                        pts  = decode_header(val)
                        out  = []
                        for b, enc in pts:
                            if isinstance(b, bytes):
                                out.append(b.decode(enc or "utf-8", errors="replace"))
                            else:
                                out.append(b)
                        return "".join(out)

                    subj      = _decode(msg["Subject"])
                    frm       = _decode(msg["From"])
                    date      = msg["Date"] or ""
                    body_text = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                charset   = part.get_content_charset() or "utf-8"
                                body_text = part.get_payload(decode=True).decode(charset, errors="replace")
                                break
                    else:
                        charset   = msg.get_content_charset() or "utf-8"
                        body_text = msg.get_payload(decode=True).decode(charset, errors="replace")
                    results.append(
                        f"【发件人】{frm}\n【主题】{subj}\n【时间】{date}\n【正文】\n{body_text[:500]}"
                    )
                return "\n\n─────\n\n".join(results) if results else "收件箱为空。"
        except Exception as e:
            return f"读取失败：{e}"

# ── read_health ───────────────────────────────────────────────
    elif cmd == "read_health":
        from pathlib import Path
        import json as _json
        f = Path("/app/health_data.json")
        if not f.exists():
            return "暂无健康数据，请先运行快捷指令同步。"
        records = _json.loads(f.read_text())
        days = data.get("days", 7)
        records = records[:days]
        lines = []
        for r in records:
            lines.append(
                f"📅 {r.get('date','')} | "
                f"步数:{r.get('steps','--')} | "
                f"热量:{r.get('active_cal','--')}千卡 | "
                f"睡眠:{r.get('sleep_start','--')}~{r.get('sleep_end','--')} | "
                f"心率 均:{r.get('hr_avg','--')} 最高:{r.get('hr_max','--')} 最低:{r.get('hr_min','--')}"
            )
        return "\n".join(lines)
    


    # ── event_create ──────────────────────────────────────────
    elif cmd == "event_create":
        name = data.get("name", "").strip()
        if not name:
            return f"错误：event_create 需要 name 参数。收到的 data: {data}"
        try:
            resp = _evt_api("POST", "/api/events", json={"name": name})
            r = resp.json()
            if resp.status_code == 409:
                return f"事件「{name}」已存在（slug: {r.get('slug')}）。"
            if resp.status_code >= 400:
                return f"错误：{r.get('error', resp.text)}"
            return f"事件「{name}」已创建。slug: {r['slug']}"
        except Exception as e:
            return f"gateway 不可达：{e}"

    # ── event_post ────────────────────────────────────────────
    elif cmd == "event_post":
        event_slug = data.get("event", "").strip()
        content = data.get("content", "").strip()
        if not event_slug or not content:
            return f"错误：event_post 需要 event 和 content。收到的 data: {data}"
        try:
            resp = _evt_api("POST", f"/api/events/{event_slug}/entries", json={"content": content})
            r = resp.json()
            if resp.status_code >= 400:
                return f"错误：{r.get('error', resp.text)}"
            return f"已写入。entry_id: {r['entry_id']}"
        except Exception as e:
            return f"gateway 不可达：{e}"

    # ── event_edit ────────────────────────────────────────────
    elif cmd == "event_edit":
        event_slug = data.get("event", "").strip()
        entry_id = data.get("entry_id", "").strip()
        content = data.get("content", "").strip()
        if not event_slug or not entry_id or not content:
            return f"错误：event_edit 需要 event、entry_id、content。收到的 data: {data}"
        try:
            resp = _evt_api("PUT", f"/api/events/{event_slug}/entries/{entry_id}", json={"content": content})
            r = resp.json()
            if resp.status_code >= 400:
                return f"错误：{r.get('error', resp.text)}"
            return f"已更新 {entry_id}。"
        except Exception as e:
            return f"gateway 不可达：{e}"

    # ── event_rm ──────────────────────────────────────────────
    elif cmd == "event_rm":
        event_slug = data.get("event", "").strip()
        entry_id = data.get("entry_id", "").strip()
        if not event_slug or not entry_id:
            return f"错误：event_rm 需要 event 和 entry_id。收到的 data: {data}"
        try:
            resp = _evt_api("DELETE", f"/api/events/{event_slug}/entries/{entry_id}")
            r = resp.json()
            if resp.status_code >= 400:
                return f"错误：{r.get('error', resp.text)}"
            return f"已删除 {entry_id}。"
        except Exception as e:
            return f"gateway 不可达：{e}"

    # ── event_list ────────────────────────────────────────────
    elif cmd == "event_list":
        event_slug = data.get("event", "").strip()
        if not event_slug:
            return f"错误：event_list 需要 event 参数。收到的 data: {data}"
        try:
            latest = int(data.get("latest", 0))
            resp = _evt_api("GET", f"/api/events/{event_slug}", params={"latest": latest} if latest else None)
            r = resp.json()
            if resp.status_code >= 400:
                return f"错误：{r.get('error', resp.text)}"
            entries = r.get("entries", [])
            if not entries:
                return f"事件「{r['name']}」暂无更新。"
            lines = [f"事件：{r['name']}（共 {r['count']} 条）\n"]
            for e in entries:
                ts = e["ts"][:16].replace("T", " ")
                edited = " (已编辑)" if e.get("updated_at") else ""
                lines.append(f"[{e['id']}] {ts}{edited}\n{e['content']}\n")
            return "\n---\n".join(lines)
        except Exception as e:
            return f"gateway 不可达：{e}"

    # ── event_drop ────────────────────────────────────────────
    elif cmd == "event_drop":
        event_slug = data.get("event", "").strip()
        if not event_slug:
            return f"错误：event_drop 需要 event 参数。收到的 data: {data}"
        try:
            resp = _evt_api("DELETE", f"/api/events/{event_slug}")
            r = resp.json()
            if resp.status_code >= 400:
                return f"错误：{r.get('error', resp.text)}"
            return f"事件「{r['name']}」已删除。"
        except Exception as e:
            return f"gateway 不可达：{e}"

    # ── event_ls ──────────────────────────────────────────────
    elif cmd == "event_ls":
        try:
            resp = _evt_api("GET", "/api/event-store")
            store = resp.json()
            if not store.get("events"):
                return "暂无挂载事件。"
            lines = []
            for slug, evt in store["events"].items():
                count = len(evt.get("entries", []))
                ts = evt.get("updated_at", "")[:16].replace("T", " ")
                lines.append(f"[{slug}] {evt['name']}（{count}条）— 最后更新 {ts}")
            return "\n".join(lines)
        except Exception as e:
            return f"gateway 不可达：{e}"

    # ── unknown ───────────────────────────────────────────────
    else:
        return (
            f"未知 cmd: {cmd}。"
            "可用：get_context / search / store_core / store_dynamic / "
            "log_turn / compress / write_diary / append_diary / "
            "read_diary / list_room / delete_core / edit_core / "
            "toy_status / toy_play / "
            "bunny_status / bunny_play / bunny_deflate / "
            "ak_status / ak_play / "
            "browser_open / browser_js / browser_click / "
            "read_health / "
            "send_email / read_email / "
            "event_create / event_post / event_edit / event_rm / "
            "event_list / event_drop / event_ls"
        )


mcp_app = mcp.sse_app()
mcp_http_app = mcp.streamable_http_app()
