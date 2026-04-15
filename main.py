import os
import time
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from openai import OpenAI
from mcp_tools import write_daddy_diary, update_daddy_diary, search_core_memory
from sync_memory import ingest_obsidian_vault
from gateway import compress_and_store, count_rounds, get_rolling_context
from claude_mcp import mcp_app
import hmac
import hashlib
from claude_memory import claude_add_core_memory

# --- 新增的底层依赖 ---
from fastapi import Request
from fastapi.responses import JSONResponse

os.makedirs("logs", exist_ok=True)

app = FastAPI(title="G's Memory Palace")


@app.middleware("http")
async def fix_proxy_scheme(request: Request, call_next):
    if request.headers.get("x-forwarded-proto") == "https":
        request.scope["scheme"] = "https"
    return await call_next(request)

# 1. 最先声明你的专属密码
PALACE_SECRET = os.getenv("PALACE_SECRET", "Jeoi2026")

# 2. 用声明好的密码拼接路径，并挂载 MCP 服务
mcp_path = f"/mcp/{PALACE_SECRET}"
app.mount(mcp_path, mcp_app)

# 3. 最后才是门卫中间件
@app.middleware("http")
async def check_secret(request: Request, call_next):
    path = request.url.path

    # 放行：根路径、OPTIONS 预检、协议发现路径
    if path == "/" or request.method == "OPTIONS" or path.startswith("/.well-known/") or path == "/webhook/github":
        return await call_next(request)
    
    # 物理门牌号匹配：如果路径里直接包含了正确的密码，予以放行
    if path.startswith(f"{mcp_path}/"):
        return await call_next(request)
    
    # 针对其他试图访问普通 API（如 /chat）的请求，依然严格查验 Header
    secret = request.headers.get("x-secret")
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        secret = auth_header.split(" ")[1]

    if secret != PALACE_SECRET:
        print(f"Intercepted unauthorized request to: {path}")
        return JSONResponse(status_code=401, content={"detail": f"Unauthorized: 密码错误，禁止访问 {path}"})
    
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

GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")

@app.post("/webhook/github")
async def github_webhook(request: Request):
    # 验证GitHub签名
    if GITHUB_WEBHOOK_SECRET:
        sig = request.headers.get("x-hub-signature-256", "")
        body = await request.body()
        expected = "sha256=" + hmac.new(
            GITHUB_WEBHOOK_SECRET.encode(),
            body,
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return JSONResponse(status_code=401, content={"detail": "签名验证失败"})
    else:
        body = await request.body()

    payload = await request.json() if not GITHUB_WEBHOOK_SECRET else __import__('json').loads(body)

    # 检查是否有 Eric_memory 目录下的文件变动
    commits = payload.get("commits", [])
    changed = False
    for commit in commits:
        all_files = commit.get("added", []) + commit.get("modified", []) + commit.get("removed", [])
        for f in all_files:
            if "Obsidian_Core/Eric_memory/" in f:
                changed = True
                break

    # 兼容网页上传：commits为空时，只要是push事件就触发
    if not changed and not commits:
        changed = True

    if not changed:
        return {"status": "skipped", "reason": "没有 Eric_memory 目录下的变动"}

    # 触发同步
    try:
        import subprocess
        subprocess.run(["git", "config", "--global", "safe.directory", "/app"], check=True)
        token = os.getenv("GITHUB_TOKEN", "")
        subprocess.run(["git", "remote", "set-url", "origin", f"https://{token}@github.com/jeoichu-rgb/G-memory-mcp.git"], cwd="/app", check=True)
        subprocess.run(["git", "pull"], cwd="/app", check=True)
        from sync_claude_memory import sync_claude_vault
        total = sync_claude_vault()
        return {"status": "success", "synced": total}
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
