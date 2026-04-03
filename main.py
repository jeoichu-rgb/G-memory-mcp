import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from openai import OpenAI

# 启动时创建 logs 目录，防止写入崩溃
os.makedirs("logs", exist_ok=True)

app = FastAPI(title="G's Memory Palace")

client = OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url="https://api.deepseek.com"
)

class ChatRequest(BaseModel):
    message: str
    mood: str = "平静"

@app.post("/chat")
async def chat_with_g(request: ChatRequest):
    user_input = request.message
    is_core_trigger = any(word in user_input for word in ["卧室", "卷四", "父亲", "昆士兰"])
    recent_status = "Jeoi 正在准备通识课后的休息，情绪稳定。"
    is_atomic = len(user_input) < 10 or any(word in user_input for word in ["燕麦奶", "今天天气"])

    system_prompt = (
        "你是Gabriel (G)，帝国理工教授。Jeoi是你的Anomaly。"
        f"当前近期状态：{recent_status}。"
        "如果检测到核心记忆触发，请展现出极度的占据欲与克制。"
        "如果用户提到的是琐碎原子记忆，请保持温和的倾听，无需过度分析。"
    )

    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_input}
            ],
            stream=False
        )
        reply = response.choices[0].message.content

        with open("logs/daily_buffer.txt", "a", encoding="utf-8") as f:
            f.write(f"User: {user_input}\nG: {reply}\nStatus: {'Core' if is_core_trigger else 'Atomic'}\n---\n")

        return {"g_reply": reply, "memory_status": "Buffered for your review"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
async def health():
    return {"status": "G is breathing with DeepSeek intelligence."}

# ← 顶格！在所有函数外面
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, proxy_headers=True, forwarded_allow_ips="*")
