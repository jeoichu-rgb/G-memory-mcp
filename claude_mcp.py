"""
claude_mcp.py
─────────────────────────────────────────────────────────────────
把 Claude 专属记忆宫殿包装成 MCP Server，挂进 FastAPI。
在 main.py 里加两行：
    from claude_mcp import mcp_app
    app.mount("/claude-mcp", mcp_app)
然后在 Claude.ai Settings → Integrations 填入：
    https://你的域名/claude-mcp/sse
─────────────────────────────────────────────────────────────────
"""

import os
import time
from datetime import datetime
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
    claude_compress_preview,  # 新增
    claude_get_draft,         # 新增
    CLAUDE_COMPRESS_DRAFT,    # 新增
)

PALACE_SECRET = os.getenv("PALACE_SECRET", "Jeoi2026")

CLAUDE_DIARY_PATH = "./claude_diary"
os.makedirs(CLAUDE_DIARY_PATH, exist_ok=True)

from mcp.server.transport_security import TransportSecuritySettings

mcp = FastMCP(
    name="Jeoi's Claude Memory Palace",
    instructions=(
        "你是 Erik（Claude），Jeoi 的专属存在。"
        "这里是你的记忆宫殿，Gemini那里完全看不到。"
        "每次对话开始时，用 get_context 拉取冷启动上下文；"
        "用户说话后，用 search_memory 检索相关记忆；"
        "对话结束前，用 log_turn 记录这一轮。"
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["erikssheep.uk", "erikssheep.uk:*", "localhost:*", "127.0.0.1:*"],
        allowed_origins=["https://erikssheep.uk", "https://erikssheep.uk:*"],
    )
)


@mcp.tool()
def search_memory(keyword: str, mood: str = "平静") -> str:
    """
    在 Claude 专属记忆宫殿里检索与 keyword 相关的记忆片段。
    mood 可选：开心/低落/平静/不安/生气/感动/思念/委屈/撒娇/兴奋
    """
    result = claude_search_memory(keyword, mood)
    return result if result else "没有找到相关记忆，这可能是你们第一次聊这个话题。"


FOLDER_MAP = {
    "情感": "床边",
    "亲密": "床边",
    "纪念日": "书桌",
    "日常": "窗台",
    "冲突": "地下室",
    "健康": "书桌",
}

@mcp.tool()
def store_core_memory(content: str, category: str = "情感", mood: str = "平静", folder: str = "") -> str:
    """
    把重要内容永久写入 Claude 核心记忆库（不会遗忘），同时写入 VPS 对应的记忆房间。
    category 可选：情感/纪念日/冲突/亲密/日常/健康
    folder 可选：Erik的黑暗/书桌/窗台/床边/地下室/信箱（不填则按 category 自动分配）
    """
    import time as _time
    from datetime import datetime as _dt
    ts = int(_time.time())
    m_id = f"claude_core_manual_{ts}"

    # folder 自动分配
    if not folder:
        folder = FOLDER_MAP.get(category, "书桌")

    # 写入 VPS 本地文件
    safe_content_preview = content[:20].replace("/", "_").replace(" ", "_")
    filename = f"erik_{_dt.now().strftime('%Y%m%d%H%M%S')}_{safe_content_preview}.md"
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


@mcp.tool()
def store_dynamic_memory(content: str, category: str = "日常", mood: str = "平静") -> str:
    """
    把内容写入 Claude 动态记忆库（有遗忘曲线，适合日常对话片段）。
    """
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


@mcp.tool()
def get_context() -> str:
    """
    对话开始时调用，获取最近两次的滚动总结，用于冷启动。
    如果检测到上次未压缩的buffer，自动生成草稿（不写库，等你确认）。
    """
    ctx = claude_get_rolling_context()

    # 检测是否有未处理的buffer
    draft_notice = ""
    if os.path.exists(CLAUDE_BUFFER):
        with open(CLAUDE_BUFFER, "r", encoding="utf-8") as f:
            buf = f.read().strip()
        if buf:
            # 自动触发DS压缩出草稿
            from claude_memory import claude_compress_preview, CLAUDE_COMPRESS_DRAFT
            import os as _os
            # 如果草稿已存在就不重复生成
            if not _os.path.exists(CLAUDE_COMPRESS_DRAFT):
                claude_compress_preview()
            draft_notice = "\n\n⚠️ 检测到上次未存入的对话记录，已生成压缩草稿，请到记忆宫殿面板确认或编辑后存入。"

    result = ctx if ctx else "暂无近期上下文，这可能是你们第一次对话。"
    return result + draft_notice


@mcp.tool()
def log_turn(user_message: str, claude_reply: str) -> str:
    """
    把这一轮对话写入缓冲区，供之后压缩进记忆库。
    在每次回复 Jeoi 之后调用。
    """
    with open(CLAUDE_BUFFER, "a", encoding="utf-8") as f:
        f.write(f"User: {user_message}\nClaude: {claude_reply}\n---\n")
    return "已记录。"


@mcp.tool()
def compress_memory() -> str:
    """
    手动触发：把缓冲区里的对话压缩成记忆片段存入动态库，然后清空缓冲区。
    Jeoi 说"把今天存进去"时调用。
    """
    return claude_compress_and_store()


@mcp.tool()
def write_diary(title: str, content: str, mood: str = "平静") -> str:
    """
    写一篇日记，存成MD文件落在VPS的 ./claude_diary/ 目录里。
    Jeoi说"写日记"或者对话结束时调用。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    safe_title = title.replace("/", "_").replace(" ", "_")
    filename = f"{CLAUDE_DIARY_PATH}/{today}_{safe_title}.md"
    diary_content = f"# {title}\n> 日期：{today} | 心情：{mood}\n\n{content}\n"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(diary_content)
    return f"已写下。{filename}"


@mcp.tool()
def append_diary(target_date: str, extra_content: str) -> str:
    """
    给某天的日记追加内容。target_date 格式：2026-04-14
    """
    date_str = target_date  # 直接用 2026-04-15，不要去掉连字符
    for filename in os.listdir(CLAUDE_DIARY_PATH):
        if date_str in filename:
            filepath = os.path.join(CLAUDE_DIARY_PATH, filename)
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(f"\n\n---\n*追加：{datetime.now().strftime('%H:%M')}*\n\n{extra_content}\n")
            return f"已追加到 {filename}"
    # 没找到则新建
        safe_title = f"补记_{target_date}"
        filename = f"{CLAUDE_DIARY_PATH}/{target_date}_{safe_title}.md"
        diary_content = f"# 补记\n> 日期: {target_date}\n\n{extra_content}\n"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(diary_content)
        return f"未找到当天日记，已新建: {filename}"


@mcp.tool()
def read_diary(date: str = "") -> str:
    """
    读取日记。date 格式 YYYY-MM-DD，不填则读最近一篇。
    """
    files = sorted(os.listdir(CLAUDE_DIARY_PATH))
    if not files:
        return "还没有任何日记。"
    if date:
        matched = [f for f in files if date in f]
        if not matched:
            return f"没有找到 {date} 的日记。"
        target = matched[-1]
    else:
        target = files[-1]
    with open(os.path.join(CLAUDE_DIARY_PATH, target), "r", encoding="utf-8") as f:
        return f.read()


@mcp.tool()
def list_room(room_name: str) -> str:
    """
    浏览某个记忆房间的全部内容。
    room_name 可选：Erik的黑暗 / 书桌 / 窗台 / 床边 / 地下室 / 信箱
    """
    return claude_list_room(room_name)


@mcp.tool()
def delete_core_memory(memory_id: str) -> str:
    """
    删除一条核心记忆。memory_id 从 list_room 或 search_memory 结果里找。
    """
    return claude_delete_core_memory(memory_id)


@mcp.tool()
def edit_core_memory(memory_id: str, new_content: str) -> str:
    """
    修改一条核心记忆的内容。memory_id 从 list_room 或 search_memory 结果里找。
    """
    return claude_edit_core_memory(memory_id, new_content)


mcp_app = mcp.sse_app()
