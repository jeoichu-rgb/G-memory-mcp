import os
import time
from claude_memory import claude_add_core_memory
import chromadb
from memory_core import GeminiEmbeddingFunction

OBSIDIAN_BASE = "./Obsidian_Core/Eric_memory"

CLAUDE_DIRS = [
    "Erik的黑暗",
    "书桌",
    "窗台",
    "床边",
    "地下室",
    "信箱",
]

CATEGORY_MAP = {
    "Erik的黑暗": "黑暗",
    "书桌": "思想",
    "窗台": "日常",
    "床边": "亲密",
    "地下室": "创伤",
    "信箱": "信件",
}

def get_collection():
    api_key = os.getenv("GEMINI_API_KEY")
    gemini_ef = GeminiEmbeddingFunction(api_key=api_key)
    client = chromadb.PersistentClient(path="./chroma_db")
    col = client.get_or_create_collection("claude_core_palace", embedding_function=gemini_ef)
    return col

def sync_claude_vault():
    col = get_collection()
    existing = col.get()
    existing_ids = set(existing["ids"])

    # 扫描现在应该有哪些ID
    expected_ids = {}  # id -> (folder, filename, filepath)
    for folder in CLAUDE_DIRS:
        folder_path = os.path.join(OBSIDIAN_BASE, folder)
        if not os.path.exists(folder_path):
            continue
        for filename in os.listdir(folder_path):
            if not filename.endswith(".md"):
                continue
            m_id = f"claude_{folder}_{filename}".replace(" ", "_").replace("/", "_")
            expected_ids[m_id] = (folder, filename, os.path.join(folder_path, filename))

    total_ingested = 0
    total_skipped = 0
    total_deleted = 0
    total_updated = 0

    # 删除：在ChromaDB里有，但md文件已经不存在了
    # 只删 obsidian_sync 来源的孤儿，mcp_manual 等手动存入的绝对不动
    existing_metadatas = dict(zip(existing["ids"], existing["metadatas"] or [{}] * len(existing["ids"])))
    orphan_ids = set()
    for eid in existing_ids:
        if eid not in expected_ids:
            meta = existing_metadatas.get(eid, {})
            if meta.get("source") == "obsidian_sync":
                orphan_ids.add(eid)
    if orphan_ids:
        col.delete(ids=list(orphan_ids))
        total_deleted = len(orphan_ids)
        print(f"已删除 {total_deleted} 条孤立记忆：{orphan_ids}")

    # 新增或更新
    existing_docs = dict(zip(existing["ids"], existing["documents"]))

    for m_id, (folder, filename, filepath) in expected_ids.items():
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        if not content.strip():
            print(f"空文件，跳过: [{folder}] - {filename}")
            continue

        if m_id in existing_ids:
            # 对比内容，变了就更新
            if existing_docs.get(m_id, "") == content:
                print(f"未变动，跳过: [{folder}] - {filename}")
                total_skipped += 1
                continue
            else:
                col.delete(ids=[m_id])
                print(f"内容变动，更新: [{folder}] - {filename}")
                total_updated += 1
        else:
            total_ingested += 1

        claude_add_core_memory(
            content=content,
            metadata={
                "category": CATEGORY_MAP.get(folder, folder),
                "filename": filename,
                "folder": folder,
                "mood": "核心印记",
                "recall_count": 0,
                "last_recalled_ts": 0,
                "source": "obsidian_sync"
            },
            memory_id=m_id
        )
        print(f"已入库: [{folder}] - {filename}")
        time.sleep(1)

    print(f"同步完成：新增 {total_ingested} 条，更新 {total_updated} 条，删除 {total_deleted} 条，跳过 {total_skipped} 条")
    return total_ingested + total_updated

if __name__ == "__main__":
    sync_claude_vault()
