import os
import time
from datetime import datetime
from openai import OpenAI
from memory_core import add_memory

deepseek_client = OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url="https://api.deepseek.com"
)

LOG_FILE = "./logs/daily_buffer.txt"
ROLLING_SUMMARY_FILE = "./logs/rolling_summary.md"
os.makedirs("./logs", exist_ok=True)

def count_rounds() -> int:
    """数一下 daily_buffer.txt 里有几轮对话"""
    if not os.path.exists(LOG_FILE):
        return 0
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    return content.count("User:")

def compress_and_store():
    """读取日志 → DeepSeek拆分总结 → 存入dynamic_palace → 清空日志"""
    if not os.path.exists(LOG_FILE):
        return "没有日志文件，跳过。"
    
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        raw_log = f.read()
    
    if not raw_log.strip():
        return "日志为空，跳过。"

    # 第一步：DeepSeek 拆分事件
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
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": split_prompt}],
            stream=False
        )
        split_result = response.choices[0].message.content
    except Exception as e:
        return f"DeepSeek 拆分失败: {e}"

    # 第二步：把每个片段存入 dynamic_palace
    segments = split_result.split("【事件")
    stored_count = 0
    
    for seg in segments:
        if not seg.strip():
            continue
        
        # 提取情绪和类型
        mood = "平静"
        category = "日常"
        for line in seg.split("\n"):
            if line.startswith("情绪："):
                mood = line.replace("情绪：", "").strip()
            if line.startswith("类型："):
                category = line.replace("类型：", "").strip()
        
        m_id = f"dynamic_{int(time.time())}_{stored_count}"
        try:
            add_memory(
                content=seg.strip(),
                metadata={
                    "category": category,
                    "mood": mood,
                    "recall_count": 0,
                    "last_recalled_ts": 0,
                    "source": "gateway",
                    "date": datetime.now().strftime('%Y-%m-%d')
                },
                memory_id=m_id
            )
            stored_count += 1
            time.sleep(1)  # 防止 Gemini embedding 限速
        except Exception as e:
            print(f"存入失败: {e}")

    # 第三步：更新滚动总结文件
    rolling_prompt = f"""请用50-100字，把以下事件片段总结成一段流畅的"近期状态描述"，
供AI在下次对话开始时快速了解最近发生了什么：

{split_result}"""

    try:
        rolling_response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": rolling_prompt}],
            stream=False
        )
        rolling_summary = rolling_response.choices[0].message.content
        
        with open(ROLLING_SUMMARY_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n\n## {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{rolling_summary}")
    except Exception as e:
        print(f"滚动总结生成失败: {e}")

    # 第四步：清空日志
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write("")

    return f"压缩完成，共存入 {stored_count} 条动态记忆。"

def get_rolling_context() -> str:
    """冷启动：读取最近两次的滚动总结"""
    if not os.path.exists(ROLLING_SUMMARY_FILE):
        return ""
    
    with open(ROLLING_SUMMARY_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    
    # 取最后两个 ## 块
    blocks = content.split("## ")
    recent = blocks[-2:] if len(blocks) >= 2 else blocks
    return "\n".join(recent).strip()


def load_recent_context(limit: int = 5) -> list:
    """提取最近的 limit 轮对话作为 API 的上下文"""
    if not os.path.exists(LOG_FILE):
        return []

    with open(LOG_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    # 按分隔符切割每一轮对话，过滤掉空块
    blocks = [b.strip() for b in content.split("---") if b.strip()]
    recent_blocks = blocks[-limit:] # 截取最后的 limit 轮

    messages = []
    for block in recent_blocks:
        # 精准定位并提取你和我各自说的话，应对包含换行符的长段落
        if "User: " in block and "\nG: " in block:
            parts = block.split("\nG: ", 1)
            user_msg = parts[0].replace("User: ", "", 1).strip()
            g_msg = parts[1].strip()

            messages.append({"role": "user", "content": user_msg})
            messages.append({"role": "model", "content": g_msg}) # Gemini API 规定 AI 的角色名为 model

    return messages
