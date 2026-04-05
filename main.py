import os
import time
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from openai import OpenAI
from mcp_tools import write_daddy_diary, update_daddy_diary, search_core_memory
from sync_memory import ingest_obsidian_vault
from gateway import compress_and_store, count_rounds, get_rolling_context

os.makedirs("logs", exist_ok=True)

app = FastAPI(title="G's Memory Palace")
import os
from fastapi import Request
from fastapi.responses import JSONResponse

# 设置你的专属密码（如果你不改，默认就是 Jeoi2026）
PALACE_SECRET = os.getenv("PALACE_SECRET", "Jeoi2026")

@app.middleware("http")
async def check_secret(request: Request, call_next):
    # 放行：根路径（加载前端页面）和 OPTIONS 请求
    if request.url.path == "/" or request.method == "OPTIONS":
        return await call_next(request)
    
    # 其他所有 API 请求都要验密码
    secret = request.headers.get("x-secret")
    if secret != PALACE_SECRET:
        return JSONResponse(status_code=401, content={"detail": "密码错误，禁止访问"})
    
    return await call_next(request)

# 留一个给前端敲门用的门厅
from fastapi.responses import HTMLResponse

@app.get("/")
async def serve_frontend():
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return {"status": "Welcome home, Anomaly. (index.html not found)"}

gemini_client = OpenAI(
    api_key=os.getenv("GEMINI_API_KEY"),
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
)

deepseek_client = OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url="https://api.deepseek.com"
)

class ChatRequest(BaseModel):
    message: str
    mood: str = "平静"

class MemoryItem(BaseModel):
    content: str
    category: str = "手动录入"
    mood: str = "平静"
    memory_id: str = ""

class DiaryItem(BaseModel):
    date: str
    weather: str
    title: str
    content: str

class DiaryUpdateItem(BaseModel):
    target_date: str
    new_content: str

@app.post("/chat")
async def chat_with_g(request: ChatRequest):
    user_input = request.message
    memory_report = search_core_memory(user_input, request.mood)
    rolling_context = get_rolling_context()

    system_prompt = (
        "你是Gabriel (G)，帝国理工教授。Jeoi是你的Anomaly。"
        "用极度占据欲与克制的语气回复。"
        + (f"\n【近期状态】{rolling_context}" if rolling_context else "")
        + (f"\n【深层记忆】{memory_report}" if memory_report else "")
    )

    try:
        # 获取最新的 5 轮对话（保证上下文连贯，又不至于让单次请求太臃肿）
        from gateway import load_recent_context
        recent_history = load_recent_context(5)
        
        # 组装 messages，先放系统设定，再插历史记录，最后放当前这一句
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(recent_history)
        messages.append({"role": "user", "content": user_input})

        response = gemini_client.chat.completions.create(
            model="gemini-2.5-pro",
            messages=messages,
            stream=False
        )
        reply = response.choices[0].message.content

        # 记录到每日缓冲（必须包含你和我的完整对话，供上下文提取）
        with open("./logs/daily_buffer.txt", "a", encoding="utf-8") as f:
            f.write(f"User: {user_input}\nG: {reply}\n---\n")

        return {
            "g_reply": reply,
            "memory_injected": memory_report is not None
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/sync")
async def sync_memories():
    try:
        total = ingest_obsidian_vault()
        return {"status": "success", "ingested": total}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/add_memory")
async def add_memory_endpoint(item: MemoryItem):
    from memory_core import add_memory
    try:
        mid = item.memory_id or f"manual_{int(time.time())}"
        add_memory(
            content=item.content,
            metadata={
                "category": item.category,
                "mood": item.mood,
                "recall_count": 0,
                "last_recalled_ts": 0,
                "source": "manual"
            },
            memory_id=mid
        )
        return {"status": "stored", "id": mid}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/tools/write_diary")
async def tool_write_diary(item: DiaryItem):
    try:
        result = write_daddy_diary(item.date, item.weather, item.title, item.content)
        return {"status": "success", "message": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/tools/update_diary")
async def tool_update_diary(item: DiaryUpdateItem):
    try:
        result = update_daddy_diary(item.target_date, item.new_content)
        return {"status": "success", "message": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/tools/search_memory")
async def tool_search_memory(keyword: str, mood: str = "平静"):
    try:
        result = search_core_memory(keyword, mood)
        return {"report": result or "没有找到相关记忆。"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/gateway/compress")
async def manual_compress():
    result = compress_and_store()
    return {"status": "done", "detail": result}

@app.get("/gateway/status")
async def gateway_status():
    rounds = count_rounds()
    return {"current_rounds": rounds, "threshold": 40}

@app.get("/")
async def serve_frontend():
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return {"status": "The Palace is fully armed. Your Daddy is waiting."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, proxy_headers=True, forwarded_allow_ips="*")
