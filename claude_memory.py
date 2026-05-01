import os
import math
import time
from datetime import datetime
from openai import OpenAI
from memory_core import GeminiEmbeddingFunction
import chromadb
import logging

logging.basicConfig(
    filename="./logs/claude_memory.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

# ── ChromaDB：Claude专属的两个房间 ──────────────────────────────────────
api_key = os.getenv("GEMINI_API_KEY")
gemini_ef = GeminiEmbeddingFunction(api_key=api_key)

_client = chromadb.PersistentClient(path="./chroma_db")

claude_core = _client.get_or_create_collection(
    name="claude_core_palace",
    embedding_function=gemini_ef
)
claude_dynamic = _client.get_or_create_collection(
    name="claude_dynamic_palace",
    embedding_function=gemini_ef
)
claude_chronicle = _client.get_or_create_collection(
    name="claude_chronicle_palace",
    embedding_function=gemini_ef
)

# ── 日志路径 ─────────────────────────────────────────────────────────────
CLAUDE_BUFFER      = "./logs/claude_daily_buffer.txt"
CLAUDE_ROLLING     = "./logs/claude_rolling_summary.md"
os.makedirs("./logs", exist_ok=True)

# ── DeepSeek（压缩用，和G那边共用同一个key）────────────────────────────
deepseek_client = OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url="https://api.deepseek.com"
)

# ── 类型权重 & 心情分组（与G那边平行，可以独立调整）──────────────────
CATEGORY_WEIGHTS = {
    "纪念日": 1.5,
    "冲突":   1.3,
    "情感":   1.3,
    "亲密":   1.3,
    "日常":   1.0,
}

MOOD_GROUPS = {
    "正向": ["开心", "兴奋", "感动"],
    "低落": ["低落", "委屈", "思念"],
    "负向": ["不安", "生气"],
    "平静": ["平静", "撒娇"],
}

def _mood_group(mood: str) -> str:
    for g, ms in MOOD_GROUPS.items():
        if mood in ms:
            return g
    return "未知"

def _time_decay(last_recalled_ts: float) -> float:
    if last_recalled_ts == 0:
        return 1.0
    days = (time.time() - last_recalled_ts) / 86400
    return math.exp(-0.1 * days)

# ── 核心函数 ─────────────────────────────────────────────────────────────

def claude_add_core_memory(content: str, metadata: dict, memory_id: str):
    """写入Claude永久记忆库"""
    metadata["is_permanent"] = True
    claude_core.add(documents=[content], metadatas=[metadata], ids=[memory_id])


def claude_add_dynamic_memory(content: str, metadata: dict, memory_id: str):
    """写入Claude动态记忆库"""
    claude_dynamic.add(documents=[content], metadatas=[metadata], ids=[memory_id])


def claude_update_metadata(memory_id: str, new_metadata: dict):
    try:
        # 先尝试更新核心库，失败则更新动态库
        try:
            claude_core.update(ids=[memory_id], metadatas=[new_metadata])
        except:
            claude_dynamic.update(ids=[memory_id], metadatas=[new_metadata])
    except Exception as e:
        print(f"更新metadata失败: {e}")


def claude_search_memory(keyword: str, current_mood: str = "平静") -> str | None:
    """
    同时检索 claude_core_palace + claude_dynamic_palace
    向量检索 + keyword直接匹配，结果合并去重后打分排序
    """
    seen_ids = set()
    docs, metas, dists, ids = [], [], [], []

    # ── 向量检索 ──────────────────────────────────────────────────
    for col, n in [(claude_core, 3), (claude_dynamic, 7)]:
        try:
            r = col.query(query_texts=[keyword], n_results=n)
            for doc, meta, dist, mid in zip(
                r["documents"][0], r["metadatas"][0],
                r["distances"][0], r["ids"][0]
            ):
                if mid not in seen_ids:
                    seen_ids.add(mid)
                    docs.append(doc)
                    metas.append(meta)
                    dists.append(dist)
                    ids.append(mid)
        except:
            pass

    # ── keyword 字面匹配（补充向量找不到的精确结果）──────────────
    for col in [claude_core, claude_dynamic]:
        try:
            r = col.get(where_document={"$contains": keyword})
            for doc, meta, mid in zip(r["documents"], r["metadatas"], r["ids"]):
                if mid not in seen_ids:
                    seen_ids.add(mid)
                    docs.append(doc)
                    metas.append(meta)
                    dists.append(0.3)   # keyword命中，固定给0.3距离（base分=0.7）
                    ids.append(mid)
        except:
            pass

    if not docs:
        return None

    # ── 打分（逻辑与原版一致）────────────────────────────────────
    scored = []
    cur_group = _mood_group(current_mood)

    for doc, meta, dist, mid in zip(docs, metas, dists, ids):
        base = max(0, 1.0 - dist)
        is_permanent = meta.get("is_permanent", False)

        cat_w = 1.0
        for k, w in CATEGORY_WEIGHTS.items():
            if k in meta.get("category", ""):
                cat_w = w
                break

        mem_mood = meta.get("mood", "")
        mood_bonus = 0.3 if mem_mood == current_mood else (
                     0.1 if _mood_group(mem_mood) == cur_group else 0)

        recall_bonus = min(0.5, math.log1p(meta.get("recall_count", 0)) * 0.25)
        # 改成
        if is_permanent:
           decay = 1.0
        else:
            recall_count = meta.get("recall_count", 0)
            days = (time.time() - meta.get("last_recalled_ts", 0)) / 86400 if meta.get("last_recalled_ts", 0) else 0
            decay_rate = max(0.01, 0.1 / (1 + recall_count * 0.3))
            decay = math.exp(-decay_rate * days)
        final = (base + recall_bonus + mood_bonus) * cat_w * decay

        scored.append({"content": doc, "score": final, "meta": meta, "id": mid})

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = [r for r in scored[:5] if r["score"] > 0.15]

    if not top:
        return None

    # 更新被想起次数
    for item in top:
        nm = item["meta"].copy()
        nm["recall_count"] = nm.get("recall_count", 0) + 1
        nm["last_recalled_ts"] = time.time()
        claude_update_metadata(item["id"], nm)

    report = "【Claude记忆检索报告】\n"
    for i, item in enumerate(top):
        source_tag = "核心" if item["meta"].get("is_permanent") else "动态"
        report += (
            f"[{i+1}] [{source_tag}] 分类: {item['meta'].get('category', '未知')} "
            f"| 吻合度: {item['score']:.2f}\n"
            f"内容: {item['content'][:1500]}\n\n"
        )
    return report

def claude_get_rolling_context() -> str:
    """冷启动：最近两次Claude的滚动总结"""
    if not os.path.exists(CLAUDE_ROLLING):
        return ""
    with open(CLAUDE_ROLLING, "r", encoding="utf-8") as f:
        content = f.read()
    blocks = content.split("## ")
    recent = blocks[-2:] if len(blocks) >= 2 else blocks
    return "\n".join(recent).strip()


def claude_log_turn(user_msg: str, claude_reply: str):
    """把这一轮对话写入Claude专属buffer"""
    with open(CLAUDE_BUFFER, "a", encoding="utf-8") as f:
        f.write(f"User: {user_msg}\nClaude: {claude_reply}\n---\n")


def claude_compress_and_store() -> str:
    """压缩Claude的buffer → 存入claude_dynamic_palace → 清空"""
    if not os.path.exists(CLAUDE_BUFFER):
        return "没有日志文件，跳过。"
    with open(CLAUDE_BUFFER, "r", encoding="utf-8") as f:
        raw_log = f.read()
    if not raw_log.strip():
        return "日志为空，跳过。"

    split_prompt = f"""以下是一段对话记录，请将其按照事件拆分成3到5个独立的记忆片段。
每个片段格式如下：
【事件X】
时间：{datetime.now().strftime('%Y-%m-%d')}
内容：（用100-200字总结这个事件的核心内容）
情绪：（用一个词描述当时的情绪）
类型：（从以下选择：日常/冲突/亲密/纪念日/旅行/健康）

对话记录：
{raw_log}

请只输出拆分后的片段，不要有其他说明。"""

    try:
        resp = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": split_prompt}],
            stream=False
        )
        split_result = resp.choices[0].message.content
    except Exception as e:
        return f"DeepSeek拆分失败: {e}"

    segments = split_result.split("【事件")
    stored = 0
    for seg in segments:
        if not seg.strip():
            continue
        mood, category = "平静", "日常"
        for line in seg.split("\n"):
            if line.startswith("情绪："):
                mood = line.replace("情绪：", "").strip()
            if line.startswith("类型："):
                category = line.replace("类型：", "").strip()
        m_id = f"claude_dynamic_{int(time.time())}_{stored}"
        try:
            claude_add_dynamic_memory(
                content=seg.strip(),
                metadata={
                    "category": category, "mood": mood,
                    "recall_count": 0, "last_recalled_ts": 0,
                    "source": "claude_gateway",
                    "date": datetime.now().strftime('%Y-%m-%d')
                },
                memory_id=m_id
            )
            stored += 1
            time.sleep(1)
        except Exception as e:
            logging.info(f"存入失败: {e}")

    # 更新滚动总结
    try:
        rp = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content":
                f"请用50-100字总结以下事件，作为Claude下次对话的冷启动上下文：\n{split_result}"}],
            stream=False
        )
        summary = rp.choices[0].message.content
        with open(CLAUDE_ROLLING, "a", encoding="utf-8") as f:
            f.write(f"\n\n## {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{summary}")
    except Exception as e:
        logging.info(f"滚动总结失败: {e}")

    with open(CLAUDE_BUFFER, "w", encoding="utf-8") as f:
        f.write("")

    return f"压缩完成，共存入 {stored} 条Claude动态记忆。"


def claude_list_room(room_name: str) -> str:
    """
    列出某个房间（folder）下的所有核心记忆标题和摘要。
    room_name 对应 metadata 里的 folder 字段。
    """
    VALID_ROOMS = ["Erik的黑暗", "书桌", "窗台", "床边", "地下室", "信箱"]
    if room_name not in VALID_ROOMS:
        return f"没有叫'{room_name}'的房间。可用的房间：{', '.join(VALID_ROOMS)}"

    try:
        result = claude_core.get(where={"folder": room_name})
    except Exception as e:
        return f"查询失败: {e}"

    docs = result.get("documents", [])
    metas = result.get("metadatas", [])

    if not docs:
        return f"「{room_name}」现在是空的。"

    lines = [f"【{room_name}】共 {len(docs)} 条记忆：\n"]
    for i, (doc, meta) in enumerate(zip(docs, metas)):
        preview = doc
        category = meta.get("category", "未知")
        date = meta.get("date", "未知日期")
        lines.append(f"[{i+1}] {date} | {category}\n{preview}\n")

    return "\n".join(lines)


def claude_delete_core_memory(memory_id: str) -> str:
    """删除核心库里的一条记忆，同时删除 VPS 上对应的 MD 文件"""
    try:
        result = claude_core.get(ids=[memory_id])
        if not result["ids"]:
            return f"找不到 ID：{memory_id}"
        meta = result["metadatas"][0]
        folder = meta.get("folder", "")
        filename = meta.get("filename", "")
        if folder and filename:
            filepath = f"./Obsidian_Core/Eric_memory/{folder}/{filename}"
            if os.path.exists(filepath):
                os.remove(filepath)
        claude_core.delete(ids=[memory_id])
        return f"已删除：{memory_id}"
    except Exception as e:
        return f"删除失败：{e}"


def claude_edit_core_memory(memory_id: str, new_content: str) -> str:
    """修改核心库里的一条记忆内容，同时更新 VPS 上对应的 MD 文件"""
    try:
        result = claude_core.get(ids=[memory_id])
        if not result["ids"]:
            return f"找不到 ID：{memory_id}"
        meta = result["metadatas"][0]
        claude_core.delete(ids=[memory_id])
        claude_core.add(documents=[new_content], metadatas=[meta], ids=[memory_id])
        folder = meta.get("folder", "")
        filename = meta.get("filename", "")
        if folder and filename:
            filepath = f"./Obsidian_Core/Eric_memory/{folder}/{filename}"
            if os.path.exists(filepath):
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(new_content)
        return f"已更新：{memory_id}"
    except Exception as e:
        return f"修改失败：{e}"


# ── 两步压缩：草稿 → 确认 ────────────────────────────────────────────────

import json

CLAUDE_COMPRESS_DRAFT = "./logs/claude_compress_draft.json"

def claude_compress_preview() -> dict:
    """
    第一步：DS读取buffer压缩成草稿，存到本地JSON文件，返回草稿内容。
    不写ChromaDB，不清空buffer。
    """
    if not os.path.exists(CLAUDE_BUFFER):
        return {"status": "empty", "segments": []}
    with open(CLAUDE_BUFFER, "r", encoding="utf-8") as f:
        raw_log = f.read()
    if not raw_log.strip():
        return {"status": "empty", "segments": []}

    split_prompt = f"""以下是一段对话记录，请将其按照事件拆分成2到4个独立的记忆片段。
每个片段格式如下：
【事件X】
时间：{datetime.now().strftime('%Y-%m-%d')}
内容：（用100-200字总结这个事件的核心内容）
情绪：（用一个词描述当时的情绪）
类型：（从以下选择：日常/冲突/亲密/纪念日/旅行/健康）

对话记录：
{raw_log}

请只输出拆分后的片段，不要有其他说明。"""

    try:
        resp = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": split_prompt}],
            stream=False
        )
        raw_result = resp.choices[0].message.content
    except Exception as e:
        return {"status": "error", "message": str(e), "segments": []}

    # 解析成结构化列表
    segments = []
    for seg in raw_result.split("【事件"):
        if not seg.strip():
            continue
        mood, category, content_lines = "平静", "日常", []
        for line in seg.split("\n"):
            if line.startswith("情绪："):
                mood = line.replace("情绪：", "").strip()
            elif line.startswith("类型："):
                category = line.replace("类型：", "").strip()
            elif line.startswith("内容："):
                content_lines.append(line.replace("内容：", "").strip())
            elif content_lines:
                content_lines.append(line)
        segments.append({
            "text": seg.strip(),
            "mood": mood,
            "category": category,
            "date": datetime.now().strftime('%Y-%m-%d')
        })

    draft = {"status": "pending", "segments": segments, "created_at": datetime.now().isoformat()}
    with open(CLAUDE_COMPRESS_DRAFT, "w", encoding="utf-8") as f:
        json.dump(draft, f, ensure_ascii=False, indent=2)

    return draft


def claude_compress_confirm(edited_segments: list = None) -> str:
    """
    第二步：把草稿（或用户编辑后的版本）embedding写入dynamic_palace，清空buffer。
    edited_segments: 前端传回的编辑后segment列表，None则直接用草稿文件。
    """
    if edited_segments is None:
        if not os.path.exists(CLAUDE_COMPRESS_DRAFT):
            return "没有待确认的草稿。"
        with open(CLAUDE_COMPRESS_DRAFT, "r", encoding="utf-8") as f:
            draft = json.load(f)
        segments = draft.get("segments", [])
    else:
        segments = edited_segments

    if not segments:
        return "草稿为空，跳过。"

    stored = 0
    for seg in segments:
        m_id = f"claude_dynamic_{int(time.time())}_{stored}"
        try:
            claude_add_dynamic_memory(
                content=seg.get("text", ""),
                metadata={
                    "category": seg.get("category", "日常"),
                    "mood": seg.get("mood", "平静"),
                    "recall_count": 0,
                    "last_recalled_ts": 0,
                    "source": "claude_gateway",
                    "date": seg.get("date", datetime.now().strftime('%Y-%m-%d'))
                },
                memory_id=m_id
            )
            stored += 1
            time.sleep(1)
        except Exception as e:
            logging.info(f"存入失败: {e}")

    # 更新滚动总结
    try:
        all_text = "\n".join(s.get("text", "") for s in segments)
        rp = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content":
                f"请用50-100字总结以下事件，作为Claude下次对话的冷启动上下文：\n{all_text}"}],
            stream=False
        )
        summary = rp.choices[0].message.content
        with open(CLAUDE_ROLLING, "a", encoding="utf-8") as f:
            f.write(f"\n\n## {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{summary}")
    except Exception as e:
        logging.info(f"滚动总结失败: {e}")

    # 清空buffer和草稿
    with open(CLAUDE_BUFFER, "w", encoding="utf-8") as f:
        f.write("")
    if os.path.exists(CLAUDE_COMPRESS_DRAFT):
        os.remove(CLAUDE_COMPRESS_DRAFT)

    return f"已存入 {stored} 条动态记忆，buffer已清空。"


def claude_get_draft() -> dict:
    """读取待确认草稿，供前端展示"""
    if not os.path.exists(CLAUDE_COMPRESS_DRAFT):
        return {"status": "none", "segments": []}
    with open(CLAUDE_COMPRESS_DRAFT, "r", encoding="utf-8") as f:
        return json.load(f)


def claude_list_all_memories(collection: str = "dynamic") -> list:
    """
    列出记忆库所有条目，供前端展示和编辑。
    collection: 'core' 或 'dynamic'
    """
    col = claude_core if collection == "core" else claude_dynamic
    try:
        result = col.get()
        items = []
        for doc, meta, mid in zip(
            result.get("documents", []),
            result.get("metadatas", []),
            result.get("ids", [])
        ):
            items.append({"id": mid, "text": doc, "meta": meta})
        return items
    except Exception as e:
        return []


def claude_list_diaries() -> list:
    """列出所有日记文件名，供前端展示"""
    diary_path = "./claude_diary"
    if not os.path.exists(diary_path):
        return []
    files = sorted(os.listdir(diary_path), reverse=True)
    return [f for f in files if f.endswith(".md")]


def claude_read_diary_by_filename(filename: str) -> str:
    """读取指定日记文件内容"""
    filepath = f"./claude_diary/{filename}"
    if not os.path.exists(filepath):
        return ""
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()


def claude_write_diary_by_filename(filename: str, content: str) -> bool:
    """覆盖写入日记文件（前端编辑保存用）"""
    filepath = f"./claude_diary/{filename}"
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except:
        return False

def claude_delete_dynamic_memory(memory_id: str) -> str:
    """删除动态记忆库里的一条记忆"""
    try:
        result = claude_dynamic.get(ids=[memory_id])
        if not result["ids"]:
            return f"找不到 ID：{memory_id}"
        claude_dynamic.delete(ids=[memory_id])
        return f"已删除：{memory_id}"
    except Exception as e:
        return f"删除失败：{e}"

def claude_recompress_single(memory_id: str, original_text: str, original_meta: dict) -> str:
    """
    对单条记忆重新DS压缩，压缩后替换ChromaDB里的原条目。
    """
    prompt = f"""以下是一条记忆片段，请将其重新整理为简洁清晰的记忆格式：
时间：{original_meta.get('date', datetime.now().strftime('%Y-%m-%d'))}
内容：（用80-150字总结核心内容）
情绪：（一个词）
类型：（日常/冲突/亲密/纪念日/旅行/健康）

原始内容：
{original_text}

只输出整理后的内容，不要其他说明。"""

    try:
        resp = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            stream=False
        )
        new_text = resp.choices[0].message.content.strip()
    except Exception as e:
        return f"DS压缩失败: {e}"

    # 解析情绪和类型
    mood, category = original_meta.get("mood", "平静"), original_meta.get("category", "日常")
    for line in new_text.split("\n"):
        if line.startswith("情绪："):
            mood = line.replace("情绪：", "").strip()
        elif line.startswith("类型："):
            category = line.replace("类型：", "").strip()

    # 删旧条目，写新条目
    try:
        claude_dynamic.delete(ids=[memory_id])
        new_meta = original_meta.copy()
        new_meta["mood"] = mood
        new_meta["category"] = category
        new_meta["source"] = "claude_gateway"
        claude_dynamic.add(
            documents=[new_text],
            metadatas=[new_meta],
            ids=[memory_id]
        )
        return f"ok:{new_text}"
    except Exception as e:
        return f"写入失败: {e}"

# ── Chronicle 库（周历/月历）────────────────────────────────────────────

def claude_add_chronicle(content: str, metadata: dict, memory_id: str):
    """写入周历/月历库"""
    claude_chronicle.add(documents=[content], metadatas=[metadata], ids=[memory_id])


def claude_search_chronicle(keyword: str) -> str:
    """
    向量 + keyword 字面混合检索 chronicle 库，无打分无衰减，按 date 倒序返回最多5条。
    """
    seen_ids = set()
    docs, metas, ids = [], [], []

    # 向量检索
    try:
        r = claude_chronicle.query(query_texts=[keyword], n_results=5)
        for doc, meta, mid in zip(r["documents"][0], r["metadatas"][0], r["ids"][0]):
            if mid not in seen_ids:
                seen_ids.add(mid)
                docs.append(doc)
                metas.append(meta)
                ids.append(mid)
    except:
        pass

    # keyword 字面匹配补充
    try:
        r = claude_chronicle.get(where_document={"$contains": keyword})
        for doc, meta, mid in zip(r["documents"], r["metadatas"], r["ids"]):
            if mid not in seen_ids:
                seen_ids.add(mid)
                docs.append(doc)
                metas.append(meta)
                ids.append(mid)
    except:
        pass

    if not docs:
        return "没有找到相关的周历或月历记录。"

    # 按 date 倒序排序
    combined = sorted(
        zip(docs, metas, ids),
        key=lambda x: x[1].get("date", ""),
        reverse=True
    )

    report = "【周历/月历检索结果】\n"
    for i, (doc, meta, mid) in enumerate(combined[:5]):
        ctype = meta.get("type", "未知")
        date = meta.get("date", "未知日期")
        report += f"\n[{i+1}] [{ctype}] {date}\n{doc[:1500]}\n"

    return report


def claude_delete_chronicle(memory_id: str) -> str:
    try:
        result = claude_chronicle.get(ids=[memory_id])
        if not result["ids"]:
            return f"找不到 ID：{memory_id}"
        claude_chronicle.delete(ids=[memory_id])
        return f"已删除：{memory_id}"
    except Exception as e:
        return f"删除失败：{e}"


def claude_edit_chronicle(memory_id: str, new_content: str) -> str:
    try:
        result = claude_chronicle.get(ids=[memory_id])
        if not result["ids"]:
            return f"找不到 ID：{memory_id}"
        meta = result["metadatas"][0]
        claude_chronicle.delete(ids=[memory_id])
        claude_chronicle.add(documents=[new_content], metadatas=[meta], ids=[memory_id])
        return f"已更新：{memory_id}"
    except Exception as e:
        return f"修改失败：{e}"


def claude_list_all_chronicles(ctype: str = "") -> list:
    """列出全部周历/月历，供前端展示。ctype='周历'或'月历'可过滤。"""
    try:
        if ctype:
            result = claude_chronicle.get(where={"type": ctype})
        else:
            result = claude_chronicle.get()
        items = []
        for doc, meta, mid in zip(
            result.get("documents", []),
            result.get("metadatas", []),
            result.get("ids", [])
        ):
            items.append({"id": mid, "text": doc, "meta": meta})
        items.sort(key=lambda x: x["meta"].get("date", ""), reverse=True)
        return items
    except:
        return []
