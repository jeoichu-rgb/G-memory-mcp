import os
import time
from memory_core import add_core_memory
from chromadb import PersistentClient

def get_existing_ids():
    """获取 core_palace 里已有的所有 ID"""
    try:
        client = PersistentClient(path="./chroma_db")
        col = client.get_collection("core_palace")
        return set(col.get()["ids"])
    except:
        return set()

def ingest_obsidian_vault():
    base_dir = "./Obsidian_Core"
    core_dirs = ["01_Jeoi的卧室", "02_Jeoi的书桌", "04_G的卧室", "G的书房（创伤共愈）"]
    existing_ids = get_existing_ids()
    total_ingested = 0
    total_skipped = 0

    for folder in core_dirs:
        folder_path = os.path.join(base_dir, folder)
        if not os.path.exists(folder_path):
            print(f"未找到房间: {folder}，跳过。")
            continue

        for filename in os.listdir(folder_path):
            if not filename.endswith(".md"):
                continue

            # 用文件名做稳定 ID，不用时间戳
            m_id = f"core_{folder}_{filename}".replace(" ", "_").replace("/", "_")

            if m_id in existing_ids:
                print(f"已存在，跳过: [{folder}] - {filename}")
                total_skipped += 1
                continue

            file_path = os.path.join(folder_path, filename)
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            add_core_memory(
                content=content,
                metadata={
                    "category": folder,
                    "filename": filename,
                    "mood": "核心印记",
                    "recall_count": 0,
                    "last_recalled_ts": 0
                },
                memory_id=m_id
            )
            total_ingested += 1
            print(f"已入库: [{folder}] - {filename}")
            time.sleep(1)

    print(f"同步完成：新入库 {total_ingested} 条，已跳过 {total_skipped} 条")
    return total_ingested

if __name__ == "__main__":
    total = ingest_obsidian_vault()
    print(f"完成。共入库 {total} 条。")
