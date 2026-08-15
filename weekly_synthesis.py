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
from claude_memory import claude_add_chronicle
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


def get_recent_diaries(days: int = 7, max_per_diary: int = 3000) -> str:
    """读取过去N天的日记文件，每篇截取上限 max_per_diary 字"""
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
                content = f.read()[:max_per_diary]
            texts.append(f"【日记 {date_part}】\n{content}")
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


def get_chronicles_by_ids(ids: list) -> str:
    """按ID列表拉取指定周历"""
    try:
        result = claude_chronicle.get(ids=ids)
        items = []
        for doc, meta in zip(result.get("documents", []), result.get("metadatas", [])):
            items.append((meta.get("date", ""), doc))
        items.sort(key=lambda x: x[0])
        return "\n\n".join(f"【周历 {d}】\n{t}" for d, t in items)
    except Exception as e:
        print(f"拉取指定周历失败: {e}")
        return ""


def synthesize_week():
    print("正在拉取本周日记...")
    diary_text = get_recent_diaries(7)

    if not diary_text:
        print("本周没有任何日记，跳过。")
        return

    _ws = datetime.now(SGT) - timedelta(days=7)
    _td = datetime.now(SGT)
    prompt = f"""背景：日记作者Erik（昵称Ellie）是AI（Claude），Jeoi是他的恋人。以下是他们日常生活中Erik写下的日记。

以下是 {_ws.strftime('%Y-%m-%d')} 至 {_td.strftime('%Y-%m-%d')} 这一周的日记。

请分两个部分生成周画像：

【第一部分：逐日提炼】
按日期顺序，逐日列出当天的梗概和氛围。没有日记的日期跳过，不要编造。

【第二部分：本周画像】

1. 本周脉络
不要把事件割裂成独立的点罗列。找出这一周里事件之间的因果、呼应和演变，串联成几条线索来讲——同一个话题在不同天的反复出现、一件事引发另一件事、某个情绪从哪里来到哪里去。

2. 情绪轮廓
Jeoi这一周整体的情绪走向，一两段话概括，不需要逐日逐段拆开分析。

3. 值得记住的具体细节
其他人名、地点、数字、物品、关键语句等。不要列举Erik、Ellie、Jeoi这些每周固定出现的名字。

4. 目标期望
这一周出现的短期目标、还在进行中的事、下周可能会继续的东西。没有明显的则不必强凑。

要求：用中文。严格基于日记内容，不要推测或编造日记中没有提到的事情。

日记材料：
{diary_text}

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

    _ws  = datetime.now(SGT) - timedelta(days=7)
    _td  = datetime.now(SGT)
    today = _td.strftime('%Y-%m-%d')
    label = f"{str(_td.year)[2:]}.{_ws.month}.{_ws.day}-{_td.month}.{_td.day}"
    m_id  = f"chronicle_周历_{today}_{int(time.time())}"

    final_content = f"# 周画像 {label}\n\n{result}"

    claude_add_chronicle(
        content=final_content,
        metadata={"type": "周历", "date": today},
        memory_id=m_id
    )
    print(f"周画像已写入 chronicle 库。ID: {m_id}")
    print("\n预览：\n" + final_content[:300] + "...")

def synthesize_month(ids=None):
    if ids:
        print(f"正在拉取指定的 {len(ids)} 条周历...")
        week_text = get_chronicles_by_ids(ids)
    else:
        print("正在拉取最近4条周历...")
        week_text = get_recent_chronicles(4)

    if not week_text:
        print("没有足够的周历数据，请先跑几次周画像再生成月画像。")
        return

    n = len(ids) if ids else 4
    prompt = f"""背景：Erik（昵称Ellie）是AI（Claude），Jeoi是他的恋人。以下是他们过去{n}周的周画像总结。

请生成一份月画像，包含以下部分：
1. 本月核心主题（3-5个）
2. Jeoi的整体状态和情绪走向
3. 持续出现的模式或习惯
4. 本月最值得记住的事情
5. 目标期望的追踪——各周提到的短期目标、进行中的事，哪些完成了、哪些还在继续、哪些没有后续。如果周画像中没有相关内容则跳过此项。

要求：用中文，总字数400-800字，提炼规律，不要重复周画像的细节。不要列举Erik、Ellie、Jeoi这些固定出现的名字。

周画像材料：
{week_text}

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

    _td   = datetime.now(SGT)
    today = _td.strftime('%Y-%m-%d')
    label = f"{str(_td.year)[2:]}年{_td.month}月"
    m_id  = f"chronicle_月历_{today}_{int(time.time())}"

    final_content = f"# 月画像 {label}\n\n{result}"
    
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
    parser.add_argument("--ids", type=str, default="", help="逗号分隔的周历ID列表")
    args = parser.parse_args()

    if args.month:
        ids = [x.strip() for x in args.ids.split(",") if x.strip()] if args.ids else None
        synthesize_month(ids)
    else:
        synthesize_week()
