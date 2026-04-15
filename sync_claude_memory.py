import os
import time
from claude_memory import claude_add_core_memory
import chromadb

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

def get_existing_ids():
    try:
        client = chromadb.PersistentClient(path="./chroma_db")
        col = client.get_collection("claude_core_palace")
        return set(col.get()["ids"])
    except:
        return set()

def sync_claude_vault():
    existing_ids = get_existing_ids()
    total_ingested = 0
    total_skipped = 0

    for folder in CLAUDE_DIRS:
        folder_path = os.path.join(OBSIDIAN_BASE, folder)
        if not os.path.exists(folder_path):
            print(f"未找到房间: {folder}，跳过。")
            continue

        for filename in os.listdir(folder_path):
            if not filename.endswith(".md"):
                continue

            m_id = f"claude_{folder}_{filename}".replace(" ", "_").replace("/", "_")

            if m_id in existing_ids:
                print(f"已存在，跳过: [{folder}] - {filename}")
                total_skipped += 1
                continue

            file_path = os.path.join(folder_path, filename)
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            if not content.strip():
                print(f"空文件，跳过: [{folder}] - {filename}")
                continue

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
            total_ingested += 1
            print(f"已入库: [{folder}] - {filename}")
            time.sleep(1)

    print(f"同步完成：新入库 {total_ingested} 条，跳过 {total_skipped} 条")
    return total_ingested

if __name__ == "__main__":
    sync_claude_vault()
