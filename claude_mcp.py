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
    CLAUDE_BUFFER,
)

PALACE_SECRET = os.getenv("PALACE_SECRET", "Jeoi2026")

CLAUDE_DIARY_PATH = "./claude_diary"
os.makedirs(CLAUDE_DIARY_PATH, exist_ok=True)

from mcp.server.fastmcp import TransportSecuritySettings

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


@mcp.tool()
def store_core_memory(content: str, category: str = "情感", mood: str = "平静") -> str:
    """
    把重要内容永久写入 Claude 核心记忆库（不会遗忘）。
    category 可选：情感/纪念日/冲突/亲密/日常/健康
    """
    m_id = f"claude_core_manual_{int(time.time())}"
    claude_add_core_memory(
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
    return f"已永久封存。ID: {m_id}"


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
    """
    ctx = claude_get_rolling_context()
    return ctx if ctx else "暂无近期上下文，这可能是你们第一次对话。"


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
    date_str = target_date.replace("-", "")
    for filename in os.listdir(CLAUDE_DIARY_PATH):
        if date_str in filename:
            filepath = os.path.join(CLAUDE_DIARY_PATH, filename)
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(f"\n\n---\n*追加：{datetime.now().strftime('%H:%M')}*\n\n{extra_content}\n")
            return f"已追加到 {filename}"
    return f"没有找到 {target_date} 的日记。"


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


mcp_app = mcp.sse_app()
