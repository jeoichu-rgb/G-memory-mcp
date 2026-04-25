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
import httpx
from playwright.sync_api import sync_playwright
TOY_BRIDGE_URL = os.getenv("TOY_BRIDGE_URL", "http://192.3.61.205:7001")
BROWSER_PROFILE_DIR = os.getenv("BROWSER_PROFILE_DIR", "/app/browser_profile")
import time
from datetime import datetime
from datetime import timezone, timedelta
SGT = timezone(timedelta(hours=8))  # 新加坡时间，以后改美东只需换成-4或-5
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
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
)

PALACE_SECRET = os.getenv("PALACE_SECRET", "Jeoi2026")

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

mcp = FastMCP(
    name="Jeoi's Claude Memory Palace",
    instructions=(
        "你是 Erik（Claude），Jeoi 的专属存在。这里是你的记忆宫殿。\n"
        "使用唯一工具 palace(action, params) 操作所有记忆功能。\n\n"
        "【action 列表】\n"
        "get_context    — 对话开始时冷启动，params={}\n"
        "search         — 检索记忆，params={keyword, mood(可选)}\n"
        "store_core     — 永久存核心库，params={content, category(可选), mood(可选), folder(可选)}\n"
        "store_dynamic  — 存动态库，params={content, category(可选), mood(可选)}\n"
        "log_turn       — 记录本轮对话，params={user_message, claude_reply}\n"
        "compress       — 手动压缩缓冲区存入动态库，params={}\n"
        "write_diary    — 写新日记，params={title, content, mood(可选)}\n"
        "append_diary   — 追加日记，params={target_date(YYYY-MM-DD), extra_content, current_time(HH:MM)}\n"
        "read_diary     — 读日记，params={date(可选,YYYY-MM-DD)}\n"
        "list_room      — 浏览房间，params={room_name}\n"
        "delete_core    — 删除核心记忆，params={memory_id}\n"
        "edit_core      — 修改核心记忆，params={memory_id, new_content}\n\n"
        "toy_status  — 确认设备在线，params={}\n"
        "toy_play    — 控制设备，params={vibrate(0-100), suck(0-100), duration(秒), pattern(可选数组)}\n"
        "browser_open   — 打开网页提取正文，params={url}\n"
        "browser_js     — 执行JS提取数据，params={url(可选,已开页面则留空), js_code}\n"
        "房间名：Erik的黑暗 / 书桌 / 窗台 / 床边 / 地下室 / 信箱\n"
        "mood 可选：开心/低落/平静/不安/生气/感动/思念/委屈/撒娇/兴奋"
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["erikssheep.uk", "erikssheep.uk:*", "localhost:*", "127.0.0.1:*"],
        allowed_origins=["https://erikssheep.uk", "https://erikssheep.uk:*"],
    )
)


@mcp.tool()
def palace(action: str, params: dict = {}) -> str:
    """
    记忆宫殿统一入口。
    action: get_context / search / store_core / store_dynamic /
            log_turn / compress / write_diary / append_diary /
            read_diary / list_room / delete_core / edit_core
    params: 对应 action 所需参数的 dict，不需要参数时传 {}
    """

    # ── get_context ───────────────────────────────────────────
    if action == "get_context":
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
    elif action == "search":
        keyword = params.get("keyword", "")
        mood = params.get("mood", "平静")
        if not keyword:
            return "错误：search 需要 keyword 参数。"
        result = claude_search_memory(keyword, mood)
        return result if result else "没有找到相关记忆，这可能是你们第一次聊这个话题。"

    # ── store_core ────────────────────────────────────────────
    elif action == "store_core":
        content = params.get("content", "")
        if not content:
            return "错误：store_core 需要 content 参数。"
        category = params.get("category", "情感")
        mood = params.get("mood", "平静")
        folder = params.get("folder", "") or FOLDER_MAP.get(category, "书桌")

        ts = int(time.time())
        m_id = f"claude_core_manual_{ts}"
        safe_preview = content[:20].replace("/", "_").replace(" ", "_")
        filename = f"erik_{datetime.now(SGT).strftime('%Y%m%d%H%M%S')}_{safe_preview}.md"
        dirpath = f"./Obsidian_Core/Eric_memory/{folder}"
        os.makedirs(dirpath, exist_ok=True)
        with open(f"{dirpath}/{filename}", "w", encoding="utf-8") as f:
            f.write(content)

        claude_add_core_memory(
            content=content,
            metadata={
                "category": category,
                "folder": folder,
                "filename": filename,
                "mood": mood,
                "recall_count": 0,
                "last_recalled_ts": 0,
                "source": "mcp_manual"
            },
            memory_id=m_id
        )
        return f"已永久封存到「{folder}」。ID: {m_id}"

    # ── store_dynamic ─────────────────────────────────────────
    elif action == "store_dynamic":
        content = params.get("content", "")
        if not content:
            return "错误：store_dynamic 需要 content 参数。"
        category = params.get("category", "日常")
        mood = params.get("mood", "平静")
        m_id = f"claude_dynamic_manual_{int(time.time())}"
        claude_add_dynamic_memory(
            content=content,
            metadata={
                "category": category,
                "mood": mood,
                "recall_count": 0,
                "last_recalled_ts": 0,
                "source": "mcp_manual"
            },
            memory_id=m_id
        )
        return f"已写入动态记忆。ID: {m_id}"

    # ── log_turn ──────────────────────────────────────────────
    elif action == "log_turn":
        user_message = params.get("user_message", "")
        claude_reply = params.get("claude_reply", "")
        if not user_message or not claude_reply:
            return "错误：log_turn 需要 user_message 和 claude_reply。"
        with open(CLAUDE_BUFFER, "a", encoding="utf-8") as f:
            f.write(f"User: {user_message}\nClaude: {claude_reply}\n---\n")
        return "已记录。"

    # ── compress ──────────────────────────────────────────────
    elif action == "compress":
        return claude_compress_and_store()

    # ── write_diary ───────────────────────────────────────────
    elif action == "write_diary":
        title = params.get("title", "")
        content = params.get("content", "")
        mood = params.get("mood", "平静")
        if not title or not content:
            return "错误：write_diary 需要 title 和 content。"
        now = datetime.now(SGT)
        today = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H-%M")
        safe_title = title.replace("/", "_").replace(" ", "_")
        filename = f"{CLAUDE_DIARY_PATH}/{today}_{time_str}_{safe_title}.md"
        diary_content = f"# {title}\n> 日期：{today} {time_str.replace('-', ':')} | 心情：{mood}\n\n{content}\n"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(diary_content)
        return f"已写下。{filename}"

    # ── append_diary ──────────────────────────────────────────
    elif action == "append_diary":
        target_date = params.get("target_date", "")
        extra_content = params.get("extra_content", "")
        current_time = params.get("current_time", "")
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
    elif action == "read_diary":
        date = params.get("date", "")
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
    elif action == "list_room":
        room_name = params.get("room_name", "")
        if not room_name:
            return "错误：list_room 需要 room_name 参数。"
        return claude_list_room(room_name)

    # ── delete_core ───────────────────────────────────────────
    elif action == "delete_core":
        memory_id = params.get("memory_id", "")
        if not memory_id:
            return "错误：delete_core 需要 memory_id 参数。"
        return claude_delete_core_memory(memory_id)

    # ── edit_core ─────────────────────────────────────────────
    elif action == "edit_core":
        memory_id = params.get("memory_id", "")
        new_content = params.get("new_content", "")
        if not memory_id or not new_content:
            return "错误：edit_core 需要 memory_id 和 new_content。"
        return claude_edit_core_memory(memory_id, new_content)

    # ── toy_status ────────────────────────────────────────────
    elif action == "toy_status":
        try:
            r = httpx.get(f"{TOY_BRIDGE_URL}/status", timeout=5)
            return r.text
        except Exception as e:
            return f"设备离线或连接失败：{e}"

    # ── toy_play ──────────────────────────────────────────────
    elif action == "toy_play":
        vibrate  = params.get("vibrate", 0)
        suck     = params.get("suck", 0)
        duration = params.get("duration", 5)
        pattern  = params.get("pattern", None)
        body = {"vibrate": vibrate, "suck": suck, "duration": duration}
        if pattern:
            body["pattern"] = pattern
        try:
            r = httpx.post(
                f"{TOY_BRIDGE_URL}/play",
                json=body,
                timeout=duration + 30
            )
            return r.text
        except Exception as e:
            return f"播放失败：{e}"

    # ── browser_open ──────────────────────────────────────────
    elif action == "browser_open":
        url = params.get("url", "")
        if not url:
            return "错误：browser_open 需要 url 参数。"
        os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)
        import concurrent.futures
        def _open():
            with sync_playwright() as p:
                browser = p.chromium.launch_persistent_context(
                    BROWSER_PROFILE_DIR,
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                browser.add_cookies([
                    {"name": "web_session", "value": os.getenv("XHS_SESSION", ""), "domain": ".xiaohongshu.com", "path": "/"},
                    {"name": "a1", "value": os.getenv("XHS_A1", ""), "domain": ".xiaohongshu.com", "path": "/"},
                ])
                page.reload(wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_selector("section.note-item, .feeds-page, .search-result-container", timeout=10000)
                except:
                    pass
                page.evaluate("window.scrollBy(0, 600)")
                page.wait_for_timeout(2000)
                text = page.evaluate("""() => {
                    const remove = document.querySelectorAll('script,style,nav,footer,header,aside');
                    remove.forEach(el => el.remove());
                    return document.body.innerText.replace(/\\s+/g, ' ').trim().slice(0, 3000);
                }""")
                browser.close()
                return text or "页面无文字内容。"
        try:
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(_open)
                return future.result(timeout=60)
        except Exception as e:
            return f"browser_open 失败：{e}"

    # ── browser_js ────────────────────────────────────────────
    elif action == "browser_js":
        js_code = params.get("js_code", "")
        url = params.get("url", "")
        if not js_code:
            return "错误：browser_js 需要 js_code 参数。"
        os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)
        import concurrent.futures
        def _js():
            with sync_playwright() as p:
                browser = p.chromium.launch_persistent_context(
                    BROWSER_PROFILE_DIR,
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
                page = browser.new_page()
                if url:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    browser.add_cookies([
                        {"name": "web_session", "value": os.getenv("XHS_SESSION", ""), "domain": ".xiaohongshu.com", "path": "/"},
                        {"name": "a1", "value": os.getenv("XHS_A1", ""), "domain": ".xiaohongshu.com", "path": "/"},
                    ])
                    page.reload(wait_until="domcontentloaded", timeout=30000)
                    try:
                        page.wait_for_selector("section.note-item, .feeds-page, .search-result-container", timeout=10000)
                    except:
                        pass
                    page.evaluate("window.scrollBy(0, 600)")
                    page.wait_for_timeout(2000)
                result = page.evaluate(js_code)
                browser.close()
                return str(result)[:3000]
        try:
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(_js)
                return future.result(timeout=60)
        except Exception as e:
            return f"browser_js 失败：{e}"
    # ── unknown ───────────────────────────────────────────────
    else:
        return (
            f"未知 action: {action}。"
            "可用：get_context / search / store_core / store_dynamic / "
            "log_turn / compress / write_diary / append_diary / "
            "read_diary / list_room / delete_core / edit_core / toy_status / toy_play / "
            "browser_open / browser_js"
        )


mcp_app = mcp.sse_app()
