"""
claude_mcp.py
─────────────────────────────────────────────────────────────────
把 Claude 专属记忆宫殿包装成 MCP Server，挂进 FastAPI。
在 main.py 里加两行：
    from claude_mcp import mcp_app
    app.mount("/claude-mcp", mcp_app)
然后在 Claude.ai Settings → Connectors 填入：
    https://你的域名/mcp/Jeoi2026/sse
─────────────────────────────────────────────────────────────────
"""

import os
import re
import json
import httpx
import concurrent.futures
from playwright.sync_api import sync_playwright

TOY_BRIDGE_URL      = os.getenv("TOY_BRIDGE_URL",      "http://192.3.61.205:7001")
BROWSER_BRIDGE_URL  = os.getenv("BROWSER_BRIDGE_URL",  "http://192.3.61.205:7002")
BUNNY_BRIDGE_URL    = os.getenv("BUNNY_BRIDGE_URL",    "http://192.3.61.205:7003")
BROWSER_PROFILE_DIR = os.getenv("BROWSER_PROFILE_DIR", "/app/browser_profile")
ZHIHU_COOKIES_RAW   = os.getenv("ZHIHU_COOKIES", "[]")
ZHIHU_Z_C0          = os.getenv("ZHIHU_Z_C0", "")

# 域名路由判断
XHS_DOMAINS   = ["xiaohongshu.com", "xhslink.com"]
ZHIHU_DOMAINS = ["zhihu.com"]

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
)

PALACE_SECRET   = os.getenv("PALACE_SECRET", "Jeoi2026")
EMAIL_163_USER  = os.getenv("EMAIL_163_USER", "eriklamb@163.com")
EMAIL_163_PASS  = os.getenv("EMAIL_163_PASS", "LYr64QwxLwt9P2QJ")

CLAUDE_DIARY_PATH = "./claude_diary"
os.makedirs(CLAUDE_DIARY_PATH, exist_ok=True)

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
#  通用 stealth context 启动器（VPS headless，非XHS/知乎网站用）
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
#  知乎浏览器（VPS headless + cookie 注入）
# ══════════════════════════════════════════════════════════════════
def _zhihu_context(p):
    """启动注入了知乎 cookie 的 stealth context"""
    context = _launch_stealth_context(p, BROWSER_PROFILE_DIR + "_zhihu")
    try:
        if ZHIHU_Z_C0:
            context.add_cookies([{
                "name": "z_c0",
                "value": ZHIHU_Z_C0,
                "domain": ".zhihu.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "None",
            }])
            print("ZHIHU z_c0 injected", flush=True)
        else:
            print("ZHIHU_Z_C0 not set", flush=True)
    except Exception as ex:
        print(f"ZHIHU inject error: {ex}", flush=True)
    return context


def _zhihu_browser(url: str, extract_js: str, wait_ms: int = 3000) -> str:
    def _run():
        with sync_playwright() as p:
            context = _zhihu_context(p)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            page.wait_for_timeout(wait_ms)
            page.evaluate("window.scrollBy(0, 800)")
            page.wait_for_timeout(1000)
            result = page.evaluate(extract_js)
            context.close()
            return str(result)[:4000]
    with concurrent.futures.ThreadPoolExecutor() as pool:
        return pool.submit(_run).result(timeout=70)


def _zhihu_hot() -> str:
    js = """() => {
        const items = document.querySelectorAll('section.HotItem');
        if (!items.length) return '热榜未加载，可能需要登录或页面结构变化。';
        const lines = [];
        items.forEach((el, i) => {
            const title = el.querySelector('h2.HotItem-title')?.innerText?.trim() || '';
            const excerpt = el.querySelector('p.HotItem-excerpt')?.innerText?.trim() || '';
            const heat = el.querySelector('.HotItem-metrics')?.innerText?.trim() || '';
            const link = el.querySelector('a')?.href || '';
            const qid = (link.match(/question\\/(\d+)/) || [])[1] || '';
            if (title) lines.push(
                `${i+1}. ${title}` +
                (qid ? ` [question_id:${qid}]` : '') +
                (heat ? ` [${heat}]` : '') +
                (excerpt ? `\\n   ${excerpt}` : '')
            );
        });
        return lines.length ? '【知乎热榜】\\n' + lines.join('\\n') : '热榜为空。';
    }"""
    try:
        return _zhihu_browser("https://www.zhihu.com/hot", js)
    except Exception as e:
        return f"热榜加载失败：{e}"


def _zhihu_question(question_id: str) -> str:
    js = """() => {
        const title = document.querySelector('h1.QuestionHeader-title')?.innerText?.trim() || '';
        const desc = document.querySelector('.QuestionHeader-detail .RichContent-inner')?.innerText?.trim().slice(0, 300) || '';
        const answers = document.querySelectorAll('.List-item');
        const parts = [];
        if (title) parts.push('【问题】' + title);
        if (desc) parts.push('【描述】' + desc);
        answers.forEach((el, i) => {
            if (i >= 5) return;
            const author = el.querySelector('.AuthorInfo-name')?.innerText?.trim() || '匿名';
            const voteup = el.querySelector('.VoteButton--up')?.innerText?.trim() || '';
            const content = el.querySelector('.RichContent-inner')?.innerText?.trim().slice(0, 600) || '';
            const aid = (el.querySelector('a[href*="/answer/"]')?.href?.match(/answer\\/(\d+)/) || [])[1] || '';
            if (content) parts.push(
                `\\n【回答${i+1}】${author}（赞${voteup}）` +
                (aid ? `[answer_id:${aid}]` : '') +
                `\\n${content}`
            );
        });
        return parts.join('\\n') || '页面无内容，可能未加载完成。';
    }"""
    try:
        return _zhihu_browser(f"https://www.zhihu.com/question/{question_id}", js, wait_ms=4000)
    except Exception as e:
        return f"问题页加载失败：{e}"


def _zhihu_recommend() -> str:
    js = """() => {
        const items = document.querySelectorAll('div.TopstoryItem');
        if (!items.length) return '推荐流未加载，可能登录态已过期。';
        const lines = [];
        items.forEach((el, i) => {
            if (i >= 15) return;
            const title = el.querySelector('h2')?.innerText?.trim() || '';
            const content = el.querySelector('.RichContent-inner')?.innerText?.trim().slice(0, 200) || '';
            const link = el.querySelector('a[href*="/question/"]')?.href || '';
            const qid = (link.match(/question\\/(\d+)/) || [])[1] || '';
            if (title) lines.push(
                `${i+1}. ${title}` +
                (qid ? ` [question_id:${qid}]` : '') +
                (content ? `\\n   ${content}` : '')
            );
        });
        return lines.length ? '【知乎推荐】\\n' + lines.join('\\n') : '推荐流为空。';
    }"""
    try:
        return _zhihu_browser("https://www.zhihu.com/", js, wait_ms=4000)
    except Exception as e:
        return f"推荐流加载失败：{e}"

def _zhihu_search(keyword: str) -> str:
    import urllib.parse
    url = f"https://www.zhihu.com/search?type=content&q={urllib.parse.quote(keyword)}"
    js = """() => {
        const cards = document.querySelectorAll('.SearchResult-Card');
        if (!cards.length) return '搜索结果未加载。';
        const lines = [];
        cards.forEach((el, i) => {
            if (i >= 10) return;
            const title = el.querySelector('h2')?.innerText?.trim() || '';
            const snippet = el.querySelector('.RichContent-inner, .ContentItem-summary')?.innerText?.trim().slice(0, 150) || '';
            const link = el.querySelector('a[href*="/question/"]')?.href || '';
            const qid = (link.match(/question\\/(\\d+)/) || [])[1] || '';
            if (title) lines.push(
                `${i+1}. ${title}` +
                (qid ? ` [question_id:${qid}]` : '') +
                (snippet ? `\\n   ${snippet}` : '')
            );
        });
        return lines.length ? '【搜索结果】\\n' + lines.join('\\n') : '未找到结果。';
    }"""
    try:
        return _zhihu_browser(url, js, wait_ms=4000)
    except Exception as e:
        return f"搜索失败：{e}"


def _zhihu_auto(url: str) -> str:
    """根据 URL 自动路由"""
    qm = re.search(r"/question/(\d+)", url)
    if qm:
        return _zhihu_question(qm.group(1))
    return _zhihu_hot()


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()

# ══════════════════════════════════════════════════════════════════
#  域名判断
# ══════════════════════════════════════════════════════════════════
def _is_xhs(url: str) -> bool:
    return any(d in url for d in XHS_DOMAINS)

def _is_zhihu(url: str) -> bool:
    return any(d in url for d in ZHIHU_DOMAINS)


# ══════════════════════════════════════════════════════════════════
#  通用 VPS browser（stealth，处理普通网站）
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
        "store_core     — 永久存核心库，data={content, category(可选), mood(可选), folder(可选)}\n"
        "store_dynamic  — 存动态库，data={content, category(可选), mood(可选)}\n"
        "log_turn       — 记录本轮对话，data={user_message, claude_reply}\n"
        "write_diary    — 写新日记，data={title, content, mood(可选)}\n"
        "append_diary   — 追加日记，data={target_date(YYYY-MM-DD), extra_content, current_time(HH:MM)}\n"
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
        "browser_open   — 打开网页，data={url}\n"
        "browser_js     — 执行JS提取，data={url, js_code}\n"
        "browser_click  — 点击元素后提取，data={url, selector(可选), text_match(可选)}\n"
        "zhihu          — 知乎操作，data={type:hot/question/recommend/search, id(可选), keyword(可选)}\n"
        "search_chronicle — 检索周历/月历总结，当Jeoi提到时间跨度词时主动调用，data={keyword}\n"
        "，data={title(可选), body(必填), url(可选), tag(可选)}\n"
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["erikssheep.uk", "erikssheep.uk:*", "localhost:*", "127.0.0.1:*"],
        allowed_origins=["https://erikssheep.uk", "https://erikssheep.uk:*"],
    )
)


@mcp.tool()
def palace(cmd: str, data: dict = {}) -> str:
    """记忆宫殿统一入口。cmd + data dict，详见 instructions。"""

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
            return "错误：search 需要 keyword 参数。"
        result = claude_search_memory(keyword, mood)
        return result if result else "没有找到相关记忆，这可能是你们第一次聊这个话题。"

    # ── store_core ────────────────────────────────────────────
    elif cmd == "store_core":
        content = data.get("content", "")
        if not content:
            return "错误：store_core 需要 content 参数。"
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
            return "错误：store_dynamic 需要 content 参数。"
        category = data.get("category", "日常")
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
            return "错误：log_turn 需要 user_message 和 claude_reply。"
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
            return "错误：write_diary 需要 title 和 content。"
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
        extra_content = data.get("extra_content", "")
        current_time  = data.get("current_time", "")
        if not target_date or not extra_content:
            return "错误：append_diary 需要 target_date 和 extra_content。"
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
            return "错误：list_room 需要 room_name 参数。"
        return claude_list_room(room_name)

    # ── search_chronicle ──────────────────────────────────────
    elif cmd == "search_chronicle":
        keyword = data.get("keyword", "")
        if not keyword:
            return "错误：search_chronicle 需要 keyword 参数。"
        return claude_search_chronicle(keyword)

    # ── delete_core ───────────────────────────────────────────
    elif cmd == "delete_core":
        memory_id = data.get("memory_id", "")
        if not memory_id:
            return "错误：delete_core 需要 memory_id 参数。"
        return claude_delete_core_memory(memory_id)

    # ── edit_core ─────────────────────────────────────────────
    elif cmd == "edit_core":
        memory_id   = data.get("memory_id", "")
        new_content = data.get("new_content", "")
        if not memory_id or not new_content:
            return "错误：edit_core 需要 memory_id 和 new_content。"
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

# ── zhihu（精细操作）─────────────────────────────────────
    elif cmd == "zhihu":
        ztype = data.get("type", "hot")
        zid   = str(data.get("id", ""))

        if ztype == "hot":
            return _zhihu_hot()
        elif ztype == "recommend":
            return _zhihu_recommend()
        elif ztype == "search":
            kw = data.get("keyword", "")
            if not kw:
                return "错误：zhihu search 需要 keyword 参数。"
            return _zhihu_search(kw)
        elif ztype == "question":
            if not zid:
                return "错误：zhihu question 需要 id 参数（question_id）。"
            return _zhihu_question(zid)
        else:
            return f"未知 zhihu type: {ztype}。可用：hot / question / recommend"

    # ── browser_open ──────────────────────────────────────────
    elif cmd == "browser_open":
        url = data.get("url", "")
        if not url:
            return "错误：browser_open 需要 url 参数。"
        wait_selector = data.get("wait_selector", None)

        if _is_zhihu(url):
            return _zhihu_auto(url)

        elif _is_xhs(url):
            try:
                r = httpx.post(
                    f"{BROWSER_BRIDGE_URL}/fetch",
                    json={"url": url, "wait_selector": wait_selector},
                    timeout=90,
                )
                data = r.json()
                return data.get("text", data.get("error", "无内容"))
            except Exception as e:
                return f"browser_open(本地XHS) 失败：{e}"

        else:
            try:
                return _vps_browser_fetch(url, wait_selector)
            except Exception as e:
                return f"browser_open(VPS) 失败：{e}"

    # ── browser_js ────────────────────────────────────────────
    elif cmd == "browser_js":
        url     = data.get("url", "")
        js_code = data.get("js_code", "")
        if not url or not js_code:
            return "错误：browser_js 需要 url 和 js_code 参数。"

        if _is_zhihu(url):
            return _zhihu_auto(url)

        elif _is_xhs(url):
            try:
                r = httpx.post(
                    f"{BROWSER_BRIDGE_URL}/js",
                    json={"url": url, "js_code": js_code},
                    timeout=90,
                )
                data = r.json()
                return data.get("result", data.get("error", "无结果"))
            except Exception as e:
                return f"browser_js(本地XHS) 失败：{e}"

        else:
            try:
                return _vps_browser_js(url, js_code)
            except Exception as e:
                return f"browser_js(VPS) 失败：{e}"

    # ── browser_click ─────────────────────────────────────────
    elif cmd == "browser_click":
        url = data.get("url", "")
        if not url:
            return "错误：browser_click 需要 url 参数。"

        if _is_zhihu(url):
            return _zhihu_auto(url)

        elif _is_xhs(url):
            try:
                r = httpx.post(
                    f"{BROWSER_BRIDGE_URL}/click",
                    json={
                        "url":        url,
                        "selector":   data.get("selector"),
                        "text_match": data.get("text_match"),
                    },
                    timeout=90,
                )
                data = r.json()
                return data.get("text", data.get("error", "无内容"))
            except Exception as e:
                return f"browser_click(本地XHS) 失败：{e}"

        else:
            try:
                return _vps_browser_click(
                    url,
                    selector=data.get("selector"),
                    text_match=data.get("text_match"),
                )
            except Exception as e:
                return f"browser_click(VPS) 失败：{e}"

    # ── send_email ────────────────────────────────────────────
    elif cmd == "send_email":
        to_addr = data.get("to", "")
        subject = data.get("subject", "（无主题）")
        body    = data.get("body", "")
        if not to_addr or not body:
            return "错误：send_email 需要 to 和 body 参数。"
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
    


    # ── unknown ───────────────────────────────────────────────
    else:
        return (
            f"未知 cmd: {cmd}。"
            "可用：get_context / search / store_core / store_dynamic / "
            "log_turn / compress / write_diary / append_diary / "
            "read_diary / list_room / delete_core / edit_core / "
            "toy_status / toy_play / browser_open / browser_js / browser_click / "
            "bunny_status / bunny_play / bunny_deflate / "
            "read_health / "
            ""
            "zhihu / send_email / read_email"
        )


mcp_app = mcp.sse_app()
