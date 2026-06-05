import os
import time
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from openai import OpenAI
from mcp_tools import write_daddy_diary, update_daddy_diary, search_core_memory
from sync_memory import ingest_obsidian_vault
from gateway import compress_and_store, count_rounds, get_rolling_context
from claude_mcp import mcp_app, mcp_http_app
import hmac
import hashlib
from claude_memory import claude_add_core_memory, claude_search_memory
from datetime import datetime, timezone, timedelta
SGT = timezone(timedelta(hours=8))

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
mcp_http_path = f"/mcp/{PALACE_SECRET}/http"
app.mount(mcp_http_path, mcp_http_app)  # Streamable HTTP for CC CLI
app.mount(mcp_path, mcp_app)  # SSE for Claude.ai web

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

        # 放行：登录页、面板页（路由内自行鉴权）、OPTIONS、webhook、MCP
        if (
            path == "/"
            or path == "/panel"
            or path == "/chat.html"
            or method == "OPTIONS"
            or path.startswith("/.well-known/")
            or path == "/webhook/github"
            or path.startswith(mcp_path)
            or path == "/api/pebbling/event"
            or path == "/sw.js"
            or path == "/manifest.json"
            or path.startswith("/icon-")
            or path == "/api/push/vapid-key"
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

# 极简登录页（不暴露任何业务代码）
from fastapi.responses import HTMLResponse

MINIMAL_LOGIN = """<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>E</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#1a1612;color:#f0ebe4;font-family:'Courier New',monospace;
display:flex;justify-content:center;align-items:center;height:100vh}
.b{width:280px;text-align:center}
h1{font-size:48px;color:#c4a8ff;margin-bottom:8px}
p{font-size:10px;letter-spacing:.35em;color:#8a7d72;margin-bottom:24px}
input{width:100%;background:#221e19;border:1px solid #3d3530;color:#f0ebe4;
font-family:inherit;font-size:13px;padding:10px 14px;outline:none;border-radius:6px;margin-bottom:12px}
input:focus{border-color:#c4a8ff}
button{width:100%;background:#2a1f42;border:1px solid #c4a8ff;color:#c4a8ff;
font-family:inherit;font-size:12px;padding:10px;cursor:pointer;border-radius:6px}
button:hover{background:#c4a8ff;color:#1a1612}
.e{font-size:11px;color:#f87171;margin-top:8px;min-height:18px}
</style></head><body><div class="b">
<h1>E</h1><p>Memory Palace</p>
<input id="p" type="password" placeholder="密码…" onkeydown="if(event.key==='Enter')go()">
<button onclick="go()">进入</button>
<div class="e" id="e"></div>
</div><script>
const K='gmp_pw';
function load(pw){
  fetch('/panel',{headers:{'x-secret':pw}}).then(r=>{
    if(r.ok) return r.text();
    throw new Error('auth');
  }).then(html=>{
    document.open();document.write(html);document.close();
  }).catch(()=>{
    localStorage.removeItem(K);
    document.querySelector('.b').style.display='block';
  })
}
const saved=localStorage.getItem(K);
if(saved){document.querySelector('.b').style.display='none';load(saved)}
function go(){
  const v=document.getElementById('p').value.trim();
  if(!v){document.getElementById('e').textContent='请输入密码';return}
  fetch('/panel',{headers:{'x-secret':v}}).then(r=>{
    if(r.ok) return r.text();
    throw new Error('auth');
  }).then(html=>{
    localStorage.setItem(K,v);
    document.open();document.write(html);document.close();
  }).catch(()=>{document.getElementById('e').textContent='密码错误'})
}
</script></body></html>"""

@app.get("/")
async def serve_login():
    return HTMLResponse(content=MINIMAL_LOGIN)

@app.get("/panel")
async def serve_panel(request: Request):
    # 路由级鉴权（中间件白名单放行了根路径，panel需自行校验）
    secret = request.headers.get("x-secret", "")
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        secret = auth.split(" ", 1)[1]
    if secret != PALACE_SECRET:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>index.html not found</h1>", status_code=500)

@app.get("/chat.html")
async def serve_chat(request: Request):
    secret = request.headers.get("x-secret", "")
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        secret = auth.split(" ", 1)[1]
    if secret != PALACE_SECRET:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    try:
        with open("chat.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>chat.html not found</h1>", status_code=500)

# ── iOS Shortcut pebbling event (proxy to cc_ws_gateway on host) ──
import json as _json
from pathlib import Path as _Path

# Docker 容器内 localhost ≠ 宿主机，用 Docker bridge gateway 访问宿主机
_GATEWAY_BASE = os.getenv("GATEWAY_URL", "http://10.0.0.1:3000")


async def _proxy_pebbling_event(payload: dict) -> dict:
    """Forward pebbling event to cc_ws_gateway on the host machine."""
    try:
        async with _httpx.AsyncClient(timeout=5) as client:
            r = await client.post(
                f"{_GATEWAY_BASE}/api/pebbling/event",
                json=payload,
            )
            return r.json()
    except Exception as e:
        # Fallback: 代理失败时返回错误但不crash
        return {"ok": False, "error": f"gateway proxy failed: {e}"}


@app.post("/api/pebbling/event")
async def record_pebbling_event_post(request: Request):
    body = await request.json()
    return await _proxy_pebbling_event(body)


@app.get("/api/pebbling/event")
async def record_pebbling_event_get(type: str = "", value: str = ""):
    if not type:
        return JSONResponse({"error": "type required"}, status_code=400)
    return await _proxy_pebbling_event({"action": type, "app": value or type})


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

# ── 健康数据接收 ────────────────────────────────────────────────────
import json as _json
from pathlib import Path

HEALTH_DATA_FILE = Path("/app/health_data.json")

@app.post("/health/update")
async def health_update(request: Request):
    try:
        body = await request.body()
        data = _json.loads(body.decode("utf-8"))
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    data["synced_at"] = datetime.now(SGT).isoformat()

    records = []
    if HEALTH_DATA_FILE.exists():
        try:
            records = _json.loads(HEALTH_DATA_FILE.read_text())
        except:
            records = []

    # 同日期覆盖，否则追加
    records = [r for r in records if r.get("date") != data.get("date")]
    records.append(data)
    records.sort(key=lambda r: r.get("date", ""), reverse=True)

    HEALTH_DATA_FILE.write_text(_json.dumps(records, ensure_ascii=False, indent=2))
    return {"status": "ok", "date": data.get("date")}

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
async def admin_edit_memory(memory_id: str, payload: MemoryEditPayload, collection: str = "dynamic"):
    if collection == "core":
        result = claude_edit_core_memory(memory_id, payload.new_content)
    else:
        from claude_memory import claude_edit_dynamic_memory
        result = claude_edit_dynamic_memory(memory_id, payload.new_content)
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

@app.get("/admin/search")
async def admin_search_memory(keyword: str, mood: str = "平静"):
    """Search memories using claude_search_memory (0.7 threshold, top 3 + diary)."""
    try:
        result = claude_search_memory(keyword, mood)
        return {"report": result or ""}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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

@app.post("/admin/synthesis")
async def admin_synthesis(payload: dict):
    import subprocess, asyncio
    stype = payload.get("type", "week")
    args = ["python3", "weekly_synthesis.py"]
    if stype == "month":
        args.append("--month")
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        output = stdout.decode() + stderr.decode()
        if proc.returncode == 0:
            return {"status": "ok", "output": output}
        else:
            return {"status": "error", "output": output}
    except asyncio.TimeoutError:
        return {"status": "error", "output": "超时（>120s）"}
    except Exception as e:
        return {"status": "error", "output": str(e)}

# ── MCP 管理 API ────────────────────────────────────────────────────
import httpx as _httpx

MCP_SETTINGS_PATH = Path(os.getenv("MCP_SETTINGS_PATH", "/opt/G-memory-mcp/.claude/settings.json"))


def _read_mcp_settings() -> dict:
    if MCP_SETTINGS_PATH.exists():
        try:
            return _json.loads(MCP_SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _write_mcp_settings(data: dict):
    MCP_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    MCP_SETTINGS_PATH.write_text(
        _json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _mcp_server_list() -> list:
    settings = _read_mcp_settings()
    servers = settings.get("mcpServers", {})
    permissions = settings.get("permissions", {}).get("allow", [])
    return [
        {
            "name": name,
            "url": cfg.get("url", ""),
            "command": cfg.get("command", ""),
            "enabled": any(p.startswith(f"mcp__{name}") for p in permissions),
        }
        for name, cfg in servers.items()
    ]


@app.get("/api/mcp")
async def api_mcp_list():
    return {"servers": _mcp_server_list()}


@app.post("/api/mcp/add")
async def api_mcp_add(request: Request):
    body = await request.json()
    name = body.get("name", "")
    url = body.get("url", "")
    if not name or not url:
        return JSONResponse(status_code=400, content={"error": "name and url required"})
    settings = _read_mcp_settings()
    settings.setdefault("mcpServers", {})[name] = {"url": url}
    perms = settings.setdefault("permissions", {}).setdefault("allow", [])
    pattern = f"mcp__{name}"
    if pattern not in perms:
        perms.append(pattern)
    _write_mcp_settings(settings)
    return {"ok": True, "servers": _mcp_server_list()}


@app.post("/api/mcp/toggle")
async def api_mcp_toggle(request: Request):
    body = await request.json()
    name = body.get("name", "")
    enabled = body.get("enabled", True)
    if not name:
        return JSONResponse(status_code=400, content={"error": "name required"})
    settings = _read_mcp_settings()
    perms = settings.setdefault("permissions", {}).setdefault("allow", [])
    pattern = f"mcp__{name}"
    if enabled:
        if pattern not in perms:
            perms.append(pattern)
    else:
        perms[:] = [p for p in perms if not p.startswith(pattern)]
    _write_mcp_settings(settings)
    return {"ok": True, "servers": _mcp_server_list()}


@app.post("/api/mcp/remove")
async def api_mcp_remove(request: Request):
    body = await request.json()
    name = body.get("name", "")
    if not name:
        return JSONResponse(status_code=400, content={"error": "name required"})
    settings = _read_mcp_settings()
    settings.get("mcpServers", {}).pop(name, None)
    perms = settings.get("permissions", {}).get("allow", [])
    perms[:] = [p for p in perms if not p.startswith(f"mcp__{name}")]
    _write_mcp_settings(settings)
    return {"ok": True, "servers": _mcp_server_list()}


@app.post("/api/mcp/test")
async def api_mcp_test(request: Request):
    body = await request.json()
    name = body.get("name", "")
    settings = _read_mcp_settings()
    cfg = settings.get("mcpServers", {}).get(name)
    if not cfg:
        return {"name": name, "ok": False, "message": "server not found"}
    url = cfg.get("url", "")
    if not url:
        return {"name": name, "ok": False, "message": "no url configured"}
    try:
        async with _httpx.AsyncClient(timeout=_httpx.Timeout(5, connect=5, read=3), verify=False) as client:
            resp = await client.get(url)
            ok = resp.status_code in (200, 301, 302, 307, 308)
            return {"name": name, "ok": ok, "message": f"HTTP {resp.status_code}"}
    except _httpx.ReadTimeout:
        return {"name": name, "ok": True, "message": "SSE 连接成功（流式端点）"}
    except _httpx.TimeoutException:
        return {"name": name, "ok": False, "message": "连接超时"}
    except Exception as e:
        return {"name": name, "ok": False, "message": str(e)}




# ══════════════════════════════════════════════════════════════════
#  Web Push 推送
# ══════════════════════════════════════════════════════════════════
from fastapi.responses import FileResponse

VAPID_PUBLIC_KEY  = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_CLAIMS      = {"sub": "mailto:eriklamb@163.com"}
PUSH_SUBS_FILE    = Path("/app/push_subscriptions.json")


def _load_push_subs() -> list:
    if PUSH_SUBS_FILE.exists():
        try:
            return _json.loads(PUSH_SUBS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_push_subs(subs: list):
    PUSH_SUBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PUSH_SUBS_FILE.write_text(
        _json.dumps(subs, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── PWA 静态文件 ──────────────────────────────────────────────────
@app.get("/sw.js")
async def serve_sw():
    return FileResponse("sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

@app.get("/manifest.json")
async def serve_manifest():
    return FileResponse("manifest.json", media_type="application/manifest+json")

@app.get("/icon-192.png")
async def serve_icon_192():
    return FileResponse("icon-192.png", media_type="image/png")

@app.get("/icon-512.png")
async def serve_icon_512():
    return FileResponse("icon-512.png", media_type="image/png")


# ── 推送 API ─────────────────────────────────────────────────────
@app.get("/api/push/vapid-key")
async def push_vapid_key():
    """返回 VAPID 公钥，前端订阅时需要。"""
    return {"publicKey": VAPID_PUBLIC_KEY}


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    """前端把 subscription 对象提交上来存库。"""
    body = await request.json()
    endpoint = body.get("endpoint", "")
    keys = body.get("keys", {})
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        return JSONResponse(status_code=400, content={"error": "invalid subscription"})

    subs = _load_push_subs()
    # 去重（用 endpoint 做唯一键）
    subs = [s for s in subs if s.get("endpoint") != endpoint]
    subs.append({
        "endpoint": endpoint,
        "keys": keys,
        "created_at": datetime.now(SGT).isoformat()
    })
    _save_push_subs(subs)
    return {"ok": True, "total": len(subs)}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request):
    """取消订阅。"""
    body = await request.json()
    endpoint = body.get("endpoint", "")
    subs = _load_push_subs()
    subs = [s for s in subs if s.get("endpoint") != endpoint]
    _save_push_subs(subs)
    return {"ok": True, "total": len(subs)}


@app.post("/api/push/send")
async def push_send(request: Request):
    """
    发送推送通知。Erik 通过 MCP 或管理面板调用。
    body: { title, body, url?, tag? }
    """
    from pywebpush import webpush, WebPushException

    if not VAPID_PRIVATE_KEY:
        return JSONResponse(status_code=500, content={"error": "VAPID_PRIVATE_KEY not configured"})

    payload = await request.json()
    notification = _json.dumps({
        "title": payload.get("title", "Erik"),
        "body":  payload.get("body", ""),
        "url":   payload.get("url", "/"),
        "tag":   payload.get("tag", "erik-push"),
    })

    subs = _load_push_subs()
    if not subs:
        return {"ok": False, "error": "没有活跃的订阅", "sent": 0}

    sent = 0
    failed = 0
    dead_endpoints = []

    for s in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": s["endpoint"],
                    "keys": s["keys"]
                },
                data=notification,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=VAPID_CLAIMS
            )
            sent += 1
        except WebPushException as e:
            if hasattr(e, 'response') and e.response is not None and e.response.status_code in (404, 410):
                dead_endpoints.append(s["endpoint"])
            failed += 1
        except Exception:
            failed += 1

    # 清理死订阅
    if dead_endpoints:
        subs = [s for s in subs if s["endpoint"] not in dead_endpoints]
        _save_push_subs(subs)

    return {"ok": sent > 0, "sent": sent, "failed": failed, "cleaned": len(dead_endpoints)}


@app.get("/api/push/status")
async def push_status():
    """查看推送订阅状态。"""
    subs = _load_push_subs()
    return {"total": len(subs), "vapid_configured": bool(VAPID_PRIVATE_KEY)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, proxy_headers=True, forwarded_allow_ips="*")
