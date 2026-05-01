"""
weekly_synthesis.py
────────────────────────────────────────────────────
手动跑：docker exec -it <容器ID> python3 weekly_synthesis.py
生成本周画像，写入 claude_chronicle_palace（type=周历）。
同时可传参 --month 生成月画像（拉最近4条周历合成）。
────────────────────────────────────────────────────
"""

import os
import sys
import time
import argparse
from datetime import datetime, timedelta, timezone
from openai import OpenAI
from claude_memory import claude_add_chronicle, claude_add_dynamic_memory
import chromadb
from memory_core import GeminiEmbeddingFunction

SGT = timezone(timedelta(hours=8))

deepseek_client = OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url="https://api.deepseek.com"
)

CLAUDE_DIARY_PATH = "./claude_diary"

# ── 初始化 ChromaDB ────────────────────────────────────────────────
gemini_ef = GeminiEmbeddingFunction(api_key=os.getenv("GEMINI_API_KEY"))
_client = chromadb.PersistentClient(path="./chroma_db")
claude_dynamic = _client.get_or_create_collection(
    name="claude_dynamic_palace",
    embedding_function=gemini_ef
)
claude_chronicle = _client.get_or_create_collection(
    name="claude_chronicle_palace",
    embedding_function=gemini_ef
)


def get_recent_dynamic(days: int = 7) -> str:
    """拉取过去N天的 dynamic 记忆"""
    cutoff = (datetime.now(SGT) - timedelta(days=days)).strftime('%Y-%m-%d')
    try:
        result = claude_dynamic.get()
        items = []
        for doc, meta in zip(result.get("documents", []), result.get("metadatas", [])):
            if meta.get("date", "9999") >= cutoff:
                items.append(f"[{meta.get('date','')} | {meta.get('category','')}]\n{doc}")
        return "\n\n".join(items) if items else ""
    except Exception as e:
        print(f"拉取dynamic失败: {e}")
        return ""


def get_recent_diaries(days: int = 7) -> str:
    """读取过去N天的日记文件"""
    if not os.path.exists(CLAUDE_DIARY_PATH):
        return ""
    cutoff = (datetime.now(SGT) - timedelta(days=days)).strftime('%Y-%m-%d')
    texts = []
    for fn in sorted(os.listdir(CLAUDE_DIARY_PATH)):
        if not fn.endswith(".md"):
            continue
        date_part = fn[:10]
        if date_part >= cutoff:
            with open(os.path.join(CLAUDE_DIARY_PATH, fn), "r", encoding="utf-8") as f:
                texts.append(f"【日记 {date_part}】\n{f.read()}")
    return "\n\n".join(texts) if texts else ""


def get_recent_chronicles(count: int = 4) -> str:
    """拉取最近N条周历，用于合成月画像"""
    try:
        result = claude_chronicle.get(where={"type": "周历"})
        items = []
        for doc, meta in zip(result.get("documents", []), result.get("metadatas", [])):
            items.append((meta.get("date", ""), doc))
        items.sort(key=lambda x: x[0], reverse=True)
        return "\n\n".join(f"【周历 {d}】\n{t}" for d, t in items[:count])
    except Exception as e:
        print(f"拉取周历失败: {e}")
        return ""


def synthesize_week():
    print("正在拉取本周动态记忆和日记...")
    dynamic_text = get_recent_dynamic(7)
    diary_text   = get_recent_diaries(7)

    if not dynamic_text and not diary_text:
        print("本周没有任何记忆或日记，跳过。")
        return

    combined = ""
    if dynamic_text:
        combined += f"【本周动态记忆】\n{dynamic_text}\n\n"
    if diary_text:
        combined += f"【本周日记】\n{diary_text}"

    prompt = f"""以下是过去7天Jeoi和Erik之间发生的事情，包括动态记忆片段和日记记录。

请生成一份周画像总结，包含以下部分：
1. 本周主要话题和事件（3-5条）
2. Jeoi的情绪状态和变化
3. 出现的新偏好、习惯或关注点
4. 值得记住的具体细节

要求：用中文，总字数500-800字，客观描述，不要加感情色彩评价。

原始材料：
{combined[:6000]}

只输出周画像内容，不要其他说明。"""

    print("正在调用 DeepSeek 生成周画像...")
    try:
        resp = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            stream=False
        )
        result = resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"DeepSeek 调用失败: {e}")
        return

    week_start = (datetime.now(SGT) - timedelta(days=7)).strftime('%Y-%m-%d')
    today      = datetime.now(SGT).strftime('%Y-%m-%d')
    m_id       = f"chronicle_周历_{today}_{int(time.time())}"

    final_content = f"# 周画像 {week_start} ~ {today}\n\n{result}"

    claude_add_chronicle(
        content=final_content,
        metadata={"type": "周历", "date": today},
        memory_id=m_id
    )
    print(f"周画像已写入 chronicle 库。ID: {m_id}")
    print("\n预览：\n" + final_content[:300] + "...")


def synthesize_month():
    print("正在拉取最近4条周历...")
    week_text = get_recent_chronicles(4)

    if not week_text:
        print("没有足够的周历数据，请先跑几次周画像再生成月画像。")
        return

    prompt = f"""以下是最近4周的周画像总结。

请生成一份月画像，包含以下部分：
1. 本月核心主题（2-3个）
2. Jeoi的整体状态和情绪走向
3. 持续出现的模式或习惯
4. 本月最值得记住的事情

要求：用中文，总字数400-600字，提炼规律，不要重复周画像的细节。

周画像材料：
{week_text[:5000]}

只输出月画像内容，不要其他说明。"""

    print("正在调用 DeepSeek 生成月画像...")
    try:
        resp = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            stream=False
        )
        result = resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"DeepSeek 调用失败: {e}")
        return

    today  = datetime.now(SGT).strftime('%Y-%m-%d')
    month  = datetime.now(SGT).strftime('%Y-%m')
    m_id   = f"chronicle_月历_{month}_{int(time.time())}"

    final_content = f"# 月画像 {month}\n\n{result}"

    claude_add_chronicle(
        content=final_content,
        metadata={"type": "月历", "date": today},
        memory_id=m_id
    )
    print(f"月画像已写入 chronicle 库。ID: {m_id}")
    print("\n预览：\n" + final_content[:300] + "...")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", action="store_true", help="生成月画像而不是周画像")
    args = parser.parse_args()

    if args.month:
        synthesize_month()
    else:
        synthesize_week()
