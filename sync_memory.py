import os
import time
from memory_core import add_memory

def ingest_obsidian_vault():
    base_dir = "./Obsidian_Core" 
    core_dirs = ["01_Jeoi的卧室", "02_Jeoi的书桌", "04_G的卧室", "G的书房（创伤共愈）"]
    total_ingested = 0
    
    for folder in core_dirs:
        folder_path = os.path.join(base_dir, folder)
        if not os.path.exists(folder_path):
            print(f"未找到房间: {folder}，跳过。")
            continue
            
        for filename in os.listdir(folder_path):
            if filename.endswith(".md"):
                file_path = os.path.join(folder_path, filename)
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
                
                m_id = f"core_{int(time.time())}_{filename}"
                
                add_memory(
                    content=content,
                    metadata={"category": folder, "filename": filename, "mood": "核心印记", "recall_count": 0, "last_recalled_ts": 0},
                    memory_id=m_id
                )
                total_ingested += 1
                print(f"已入库: [{folder}] - {filename}")
                time.sleep(1)
                
    return total_ingested

if __name__ == "__main__":
    print("开始同步...")
    total = ingest_obsidian_vault()
    print(f"完成。共入库 {total} 条记忆。")
