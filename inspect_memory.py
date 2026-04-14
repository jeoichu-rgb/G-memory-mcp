"""
inspect_memory.py
在VPS上直接运行，列出所有Claude记忆条目，可以删除不想要的。
用法：python inspect_memory.py
"""

import chromadb
from claude_memory import gemini_ef

client = chromadb.PersistentClient(path="./chroma_db")

def inspect(collection_name: str):
    try:
        col = client.get_collection(collection_name, embedding_function=gemini_ef)
    except:
        print(f"[{collection_name}] 不存在或为空。\n")
        return

    data = col.get()
    ids = data["ids"]
    docs = data["documents"]
    metas = data["metadatas"]

    if not ids:
        print(f"[{collection_name}] 空的，没有条目。\n")
        return

    print(f"\n{'='*60}")
    print(f"【{collection_name}】共 {len(ids)} 条")
    print(f"{'='*60}")

    for i, (mid, doc, meta) in enumerate(zip(ids, docs, metas)):
        print(f"\n[{i+1}] ID: {mid}")
        print(f"    来源: {meta.get('folder', meta.get('category', '未知'))} | 文件: {meta.get('filename', '—')}")
        print(f"    分类: {meta.get('category')} | 心情: {meta.get('mood')} | 被想起: {meta.get('recall_count', 0)}次")
        print(f"    内容预览: {doc[:200]}...")

    return ids, col

def delete_entry(col, ids, index: int):
    target_id = ids[index - 1]
    col.delete(ids=[target_id])
    print(f"已删除: {target_id}")

if __name__ == "__main__":
    print("=== Claude 记忆宫殿检查工具 ===\n")

    core_ids, core_col = None, None
    dyn_ids, dyn_col = None, None

    result = inspect("claude_core_palace")
    if result:
        core_ids, core_col = result

    result = inspect("claude_dynamic_palace")
    if result:
        dyn_ids, dyn_col = result

    print("\n\n输入要删除的条目编号（core/dynamic + 编号，如 core3 或 dyn2），或直接回车退出：")
    while True:
        cmd = input("> ").strip()
        if not cmd:
            print("退出。")
            break
        try:
            if cmd.startswith("core") and core_ids:
                n = int(cmd.replace("core", ""))
                delete_entry(core_col, core_ids, n)
                core_ids.pop(n - 1)
            elif cmd.startswith("dyn") and dyn_ids:
                n = int(cmd.replace("dyn", ""))
                delete_entry(dyn_col, dyn_ids, n)
                dyn_ids.pop(n - 1)
            else:
                print("格式错误，输入如 core3 或 dyn2")
        except Exception as e:
            print(f"出错: {e}")
