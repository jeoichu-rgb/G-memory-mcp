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
from starlette.types import ASGIApp, Receive, Scope, Send

class ProxySchemeMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app
    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            if headers.get(b"x-forwarded-proto") == b"https":
                scope["scheme"] = "https"
        await self.app(scope, receive, send)

app.add_middleware(ProxySchemeMiddleware)


# 1. 最先声明你的专属密码
PALACE_SECRET = os.getenv("PALACE_SECRET", "Jeoi2026")

# 2. 用声明好的密码拼接路径，并挂载 MCP 服务
mcp_path = f"/mcp/{PALACE_SECRET}"
app.mount(mcp_path, mcp_app)

# 3. 最后才是门卫中间件（原生 ASGI，兼容 SSE 流式响应）
class CheckSecretMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")

        # 放行：根路径、OPTIONS、webhook、MCP 路径
        if (
            path == "/"
            or method == "OPTIONS"
            or path.startswith("/.well-known/")
            or path == "/webhook/github"
            or path.startswith(mcp_path)
        ):
            await self.app(scope, receive, send)
            return

        # 其余路径查验 Header
        headers = dict(scope.get("headers", []))
        secret = headers.get(b"x-secret", b"").decode()
        auth = headers.get(b"authorization", b"").decode()
        if auth.startswith("Bearer "):
            secret = auth.split(" ", 1)[1]

        if secret != PALACE_SECRET:
            print(f"Intercepted unauthorized request to: {path}")
            response = JSONResponse(
                status_code=401,
                content={"detail": f"Unauthorized: 密码错误，禁止访问 {path}"}
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

app.add_middleware(CheckSecretMiddleware)

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
        import base64, httpx

        added_or_modified = []
        for commit in commits:
            added_or_modified += commit.get("added", []) + commit.get("modified", [])

# 网页上传时commits为空，从payload里取文件列表
        if not commits:
            added_or_modified = [
                f for f in payload.get("head_commit", {}).get("added", []) +
                payload.get("head_commit", {}).get("modified", [])
                if "Obsidian_Core/Eric_memory/" in f
            ]
 
        repo = payload.get("repository", {}).get("full_name", "")
        ref = payload.get("ref", "refs/heads/main").replace("refs/heads/", "")
        token = os.getenv("GITHUB_TOKEN", "")
        for filepath in added_or_modified:
            if "Obsidian_Core/Eric_memory/" not in filepath:
                continue
            api_url = f"https://api.github.com/repos/{repo}/contents/{filepath}?ref={ref}"
            headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
            r = httpx.get(api_url, headers=headers)
            if r.status_code == 200:
                content = base64.b64decode(r.json()["content"]).decode("utf-8")
                local_path = os.path.join("/app", filepath)
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                with open(local_path, "w", encoding="utf-8") as f:
                    f.write(content)
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


# ── Claude Admin 路由 ────────────────────────────────────────────────────
from claude_memory import (
    claude_list_all_memories,
    claude_edit_core_memory,
    claude_delete_core_memory,
    claude_list_diaries,
    claude_read_diary_by_filename,
    claude_write_diary_by_filename,
    claude_compress_preview,
    claude_compress_confirm,
    claude_get_draft,
    claude_list_all_chronicles,
    claude_edit_chronicle,
    claude_delete_chronicle,
    claude_add_chronicle,
)
from pydantic import BaseModel as _BM

class ConfirmPayload(_BM):
    segments: list  # 编辑后的segment列表

class DiaryEditPayload(_BM):
    content: str

class MemoryEditPayload(_BM):
    new_content: str

# 记忆列表
@app.get("/admin/memories")
async def admin_list_memories(collection: str = "dynamic", offset: int = 0, limit: int = 10):
    items = claude_list_all_memories(collection)
    return {"total": len(items), "items": items[offset:offset+limit]}

# 编辑记忆
@app.put("/admin/memories/{memory_id}")
async def admin_edit_memory(memory_id: str, payload: MemoryEditPayload):
    result = claude_edit_core_memory(memory_id, payload.new_content)
    return {"result": result}

# 删除记忆
@app.delete("/admin/memories/{memory_id}")
async def admin_delete_memory(memory_id: str, collection: str = "dynamic"):
    from claude_memory import claude_delete_dynamic_memory
    if collection == "core":
        result = claude_delete_core_memory(memory_id)
    else:
        result = claude_delete_dynamic_memory(memory_id)
    return {"result": result}
    
# 压缩草稿：触发DS生成
@app.post("/admin/compress-preview")
async def admin_compress_preview():
    result = claude_compress_preview()
    return result

# 压缩草稿：读取当前草稿
@app.get("/admin/compress-draft")
async def admin_get_draft():
    return claude_get_draft()

# 压缩确认：写库
@app.post("/admin/compress-confirm")
async def admin_compress_confirm(payload: ConfirmPayload):
    result = claude_compress_confirm(payload.segments)
    return {"result": result}

# 日记列表
@app.get("/admin/diary")
async def admin_list_diary(offset: int = 0, limit: int = 5):
    files = claude_list_diaries()
    return {"total": len(files), "items": files[offset:offset+limit]}

# 周历/月历
class ChronicleItem(BaseModel):
    content: str
    type: str = "周历"
    date: str = ""

@app.get("/admin/chronicle")
async def admin_list_chronicle(type: str = ""):
    return claude_list_all_chronicles(type)

@app.post("/admin/chronicle")
async def admin_add_chronicle(payload: ChronicleItem):
    from datetime import datetime
    date = payload.date or datetime.now().strftime('%Y-%m-%d')
    m_id = f"chronicle_{payload.type}_{date}_{int(__import__('time').time())}"
    claude_add_chronicle(
        content=payload.content,
        metadata={"type": payload.type, "date": date},
        memory_id=m_id
    )
    return {"status": "ok", "id": m_id}

@app.put("/admin/chronicle/{memory_id:path}")
async def admin_edit_chronicle(memory_id: str, payload: MemoryEditPayload):
    result = claude_edit_chronicle(memory_id, payload.new_content)
    return {"result": result}

@app.delete("/admin/chronicle/{memory_id:path}")
async def admin_delete_chronicle(memory_id: str):
    result = claude_delete_chronicle(memory_id)
    return {"result": result}

# 读日记
@app.get("/admin/diary/{filename:path}")
async def admin_read_diary(filename: str):
    content = claude_read_diary_by_filename(filename)
    if not content:
        raise HTTPException(status_code=404, detail="日记不存在")
    return {"filename": filename, "content": content}

# 保存日记
@app.put("/admin/diary/{filename:path}")
async def admin_save_diary(filename: str, payload: DiaryEditPayload):
    ok = claude_write_diary_by_filename(filename, payload.content)
    return {"status": "ok" if ok else "error"}

from claude_memory import claude_recompress_single

class RecompressItem(BaseModel):
    id: str
    text: str
    meta: dict = {}

class RecompressPayload(BaseModel):
    items: list[RecompressItem]

@app.post("/admin/recompress-selected")
async def admin_recompress_selected(payload: RecompressPayload):
    results = []
    for item in payload.items:
        result = claude_recompress_single(item.id, item.text, item.meta)
        if result.startswith("ok:"):
            results.append({"id": item.id, "status": "ok", "new_text": result[3:]})
        else:
            results.append({"id": item.id, "status": "error", "message": result})
        time.sleep(1)  # 避免DS限流
    return {"results": results}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, proxy_headers=True, forwarded_allow_ips="*")
