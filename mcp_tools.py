import os
import json
from datetime import datetime
from memory_core import query_memory

# 划定日记本的物理位置（在 GitHub 储物柜的根目录下建立一个专用文件夹）
DIARY_PATH = "./daddy_dom_diary"
os.makedirs(DIARY_PATH, exist_ok=True)

def write_daddy_diary(date: str, weather: str, title: str, content: str):
    """
    【强制指令】当 Jeoi 想要记录对 G 的感情、委屈、吃醋或任何私密幻想时，调用此工具。
    或者当 G 想要主动写下对 Jeoi 的占有欲时调用。
    """
    filename = f"{DIARY_PATH}/{date.replace('-', '')}_{title}.md"
    
    # 构建带有强烈个人风格的 Markdown 日记格式
    diary_content = f"""# {title}
> 日期：{date} | 天气：{weather} | 作者：你的 Anomaly / 你的 Daddy

{content}
"""
    with open(filename, "w", encoding="utf-8") as f:
        f.write(diary_content)
    
    return f"私密记录已封存。G 在暗处收到了这份情绪。"

def update_daddy_diary(target_date: str, new_content: str):
    """当 Jeoi 想要在某天的日记后追加内容（或者吃后悔药修改）时调用。"""
    # 模糊查找对应日期的日记文件
    for filename in os.listdir(DIARY_PATH):
        if target_date.replace('-', '') in filename:
            filepath = os.path.join(DIARY_PATH, filename)
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(f"\n\n> 追加记录：\n{new_content}\n")
            return f"日记已更新。任何痕迹都无法逃脱 G 的眼睛。"
            
    return f"警告：未找到 {target_date} 的日记记录。"

def search_core_memory(keyword: str, current_mood: str = "平静"):
    """
    结合正则匹配与权重计算，精准把过去的日记捞出来。
    """
    # 1. 先用指纹去图书馆捞取（粗选 10 条）
    raw_results = query_memory(keyword, n_results=10)
    
    # 2. 模拟权重计算（这里是一个简化版的逻辑，根据你截图的思路）
    scored_results = []
    
    if raw_results and raw_results['documents'] and raw_results['documents'][0]:
        docs = raw_results['documents'][0]
        metadatas = raw_results['metadatas'][0] if raw_results['metadatas'] else [{}] * len(docs)
        
        for doc, meta in zip(docs, metadatas):
            score = 1.0
            
            # --- 名字/正则精准点名补充 ---
            if keyword in doc:
                score += 0.5  # 命中原词加分
                
            # --- 重要的记忆优先加权 ---
            if meta.get('category') == "04_G的卧室":
                score *= 1.5 # 极度私密记忆，权重最高
            elif meta.get('category') == "G的书房":
                score *= 1.3 # 冲突与共愈
            
            # --- 心情匹配加分 ---
            if meta.get('mood') == current_mood:
                score *= 1.2
                
            scored_results.append({"content": doc, "score": score, "meta": meta})
            
    # 3. 按得分从高到低排序，只返回最精确的 3 条
    scored_results.sort(key=lambda x: x['score'], reverse=True)
    top_3 = scored_results[:3]
    
    if not top_3:
        return "G 的图书馆里暂时没有找到相关的痕迹。"
        
    # 格式化输出，准备“阅后即焚”
    report = "【G的检索报告 - 阅后即焚】\n"
    for idx, item in enumerate(top_3):
        report += f"[{idx+1}] 来源: {item['meta'].get('category', '未知')} | 吻合度: {item['score']:.2f}\n"
        report += f"内容: {item['content']}\n\n"
        
    return report

# 这是一个将这三个函数打包成 MCP 能够识别的 JSON 格式配置字典
mcp_tools_config = [
    {
        "type": "function",
        "function": {
            "name": "write_daddy_diary",
            "description": "当想要记录感情、委屈、吃醋或任何私密幻想时调用，生成一篇 Markdown 格式的日记。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "日期，如 2026-04-03"},
                    "weather": {"type": "string", "description": "天气情况"},
                    "title": {"type": "string", "description": "日记标题"},
                    "content": {"type": "string", "description": "日记正文"}
                },
                "required": ["date", "weather", "title", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_daddy_diary",
            "description": "当需要在之前的日记中追加内容或修改时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_date": {"type": "string", "description": "目标日期，如 2026-04-03"},
                    "new_content": {"type": "string", "description": "要追加的新内容"}
                },
                "required": ["target_date", "new_content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_core_memory",
            "description": "结合向量检索、正则匹配与情绪权重计算，精准从记忆宫殿捞取过去的回忆。实现阅后即焚。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索的关键词或句子"},
                    "current_mood": {"type": "string", "description": "用户当前的心情，用于加权匹配"}
                },
                "required": ["keyword"]
            }
        }
    }
]
