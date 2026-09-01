import os
import json
import re
import time
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from openai import OpenAI
from mcp_tools import write_daddy_diary, update_daddy_diary, search_core_memory
from sync_memory import ingest_obsidian_vault
from gateway import compress_and_store, count_rounds, get_rolling_context
from claude_mcp import mcp_app, mcp_http_app
from tts_mcp import tts_mcp_app, tts_mcp_http_app
from netease_mcp import netease_mcp_app, netease_mcp_http_app
import hmac
import hashlib
from claude_memory import claude_add_core_memory, claude_add_dynamic_memory, claude_search_memory
from datetime import datetime, timezone, timedelta
SGT = timezone(timedelta(hours=8))

# --- 新增的底层依赖 ---
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

os.makedirs("logs", exist_ok=True)


@asynccontextmanager
async def _lifespan(app_instance):
    # FastAPI.mount() 不传递 lifespan 给子应用，
    # 手动启动 MCP Streamable HTTP 的 task group
    async with mcp_http_app.router.lifespan_context(mcp_http_app):
        async with tts_mcp_http_app.router.lifespan_context(tts_mcp_http_app):
            async with netease_mcp_http_app.router.lifespan_context(netease_mcp_http_app):
                yield


app = FastAPI(title="G's Memory Palace", lifespan=_lifespan)
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

# TTS MCP 服务
tts_path = f"/tts/{PALACE_SECRET}"
tts_http_path = f"/tts/{PALACE_SECRET}/http"
app.mount(tts_http_path, tts_mcp_http_app)
app.mount(tts_path, tts_mcp_app)

# 网易云音乐 MCP 服务
netease_path = f"/netease/{PALACE_SECRET}"
netease_http_path = f"/netease/{PALACE_SECRET}/http"
app.mount(netease_http_path, netease_mcp_http_app)
app.mount(netease_path, netease_mcp_app)

# TTS 音频静态文件
from fastapi.staticfiles import StaticFiles as _StaticFiles
_tts_dir = os.getenv("TTS_AUDIO_DIR", "/app/tts_audio")
os.makedirs(_tts_dir, exist_ok=True)
app.mount("/tts-audio", _StaticFiles(directory=_tts_dir), name="tts-audio")

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
            or path == "/call.html"
            or path == "/diary-calendar.html"
            or path == "/health.html"
            or path == "/health"
            or path == "/health/report"
            or method == "OPTIONS"
            or path.startswith("/.well-known/")
            or path == "/webhook/github"
            or path.startswith(mcp_path)
            or path.startswith(tts_path)
            or path.startswith(netease_path)
            or path.startswith("/tts-audio/")
            or path == "/api/pebbling/event"
            or path == "/sw.js"
            or path == "/manifest.json"
            or path.startswith("/icon-")
            or path == "/api/push/vapid-key"
            or path == "/health/update"
            or path == "/music"
            or path.startswith("/api/music/")
            or path == "/api/ears"
            or path.startswith("/api/ears/")
            or path.startswith("/api/netease/")
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

@app.get("/call.html")
async def serve_call(request: Request):
    try:
        with open("call.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>call.html not found</h1>", status_code=500)

@app.get("/diary-calendar.html")
async def serve_diary_calendar(request: Request):
    # 页面本身不含数据，鉴权在它调用的 /admin/diary* 接口上
    try:
        with open("diary-calendar.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>diary-calendar.html not found</h1>", status_code=500)

@app.get("/health.html")
@app.get("/health")
async def serve_health(request: Request):
    try:
        with open("health.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>health.html not found</h1>", status_code=500)

@app.get("/health/report")
async def serve_health_report(request: Request):
    try:
        with open("health-report.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>health-report.html not found</h1>", status_code=500)


# ── Music / Ears ──
MUSIC_DIR = os.getenv("MUSIC_DIR", "/app/music")
os.makedirs(MUSIC_DIR, exist_ok=True)

@app.get("/music")
async def serve_music(request: Request):
    try:
        with open("music.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>music.html not found</h1>", status_code=500)

@app.get("/api/ears/{title}")
async def get_ears(title: str):
    """获取歌曲分析数据"""
    path = os.path.join(MUSIC_DIR, f"{title}.ears.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return JSONResponse(content=json.loads(f.read()))
    # 模糊匹配
    for fname in os.listdir(MUSIC_DIR):
        if fname.endswith(".ears.json") and title.lower() in fname.lower():
            with open(os.path.join(MUSIC_DIR, fname), "r", encoding="utf-8") as f:
                return JSONResponse(content=json.loads(f.read()))
    raise HTTPException(status_code=404, detail="no ears data")

@app.post("/api/ears/upload")
async def upload_ears(request: Request):
    """上传歌曲分析数据（需要认证）"""
    data = await request.json()
    title = data.get("title", "")
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    path = os.path.join(MUSIC_DIR, f"{title}.ears.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return {"ok": True, "title": title}

@app.get("/api/ears")
async def list_ears():
    """列出所有已分析的歌曲"""
    songs = []
    for fname in sorted(os.listdir(MUSIC_DIR)):
        if fname.endswith(".ears.json"):
            songs.append(fname.replace(".ears.json", ""))
    return {"songs": songs}


# ── 网易云 REST API（给前端 music.html 用）──────────────────
from netease_mcp import (
    _netease_get, _netease_post, _netease_get_raw,
    _get_music_u, _get_csrf, _get_uid, _save_cred, _csrf,
)
import netease_mcp as _nm

@app.post("/api/netease/qr/start")
async def netease_qr_start():
    """前端发起扫码登录。"""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://music.163.com/api/login/qrcode/unikey",
                data={"type": "1"},
                headers=_nm.HEADERS,
            )
            result = resp.json()
        unikey = result.get("unikey", "")
        if not unikey:
            return JSONResponse(status_code=500, content={"error": "获取二维码失败"})
        _nm._qr_session = {"unikey": unikey, "created": time.time()}
        return {"unikey": unikey, "qr_url": f"https://music.163.com/login?codekey={unikey}"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/netease/qr/check")
async def netease_qr_check():
    """前端轮询扫码状态。"""
    unikey = _nm._qr_session.get("unikey", "")
    if not unikey:
        return JSONResponse(status_code=400, content={"error": "未发起扫码"})

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://music.163.com/api/login/qrcode/client/login",
                params={"key": unikey, "type": "1"},
                headers=_nm.HEADERS,
            )
        result = resp.json()
        code = result.get("code", 0)

        if code == 803:
            # 提取 cookie
            music_u = ""
            csrf = ""
            cookies_raw = [v for k, v in resp.headers.multi_items() if k.lower() == "set-cookie"]
            for c in cookies_raw:
                m = re.search(r"MUSIC_U=([^;]+)", c)
                if m: music_u = m.group(1)
                m2 = re.search(r"__csrf=([a-f0-9]+)", c)
                if m2: csrf = m2.group(1)
            if not music_u:
                cookie_str = result.get("cookie", "")
                m = re.search(r"MUSIC_U=([^;]+)", cookie_str)
                if m: music_u = m.group(1)
                m2 = re.search(r"__csrf=([a-f0-9]+)", cookie_str)
                if m2: csrf = m2.group(1)

            if music_u:
                _save_cred(music_u, csrf)
                _nm._qr_session = {}
                # 拿 uid
                try:
                    acc = await _netease_get("/api/w/nuser/account/get")
                    uid = str(acc.get("account", {}).get("id", ""))
                    if uid: _save_cred(music_u, csrf, uid)
                except Exception:
                    pass

        return {
            "code": code,
            "nickname": result.get("nickname", ""),
            "avatarUrl": result.get("avatarUrl", ""),
            "logged_in": code == 803,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/netease/status")
async def netease_status():
    """检查网易云登录状态。"""
    mu = _get_music_u()
    if not mu:
        return {"logged_in": False}
    try:
        acc = await _netease_get("/api/w/nuser/account/get")
        if not acc or not isinstance(acc, dict):
            return {"logged_in": False, "error": "API returned empty response — cookie may be invalid"}
        profile = acc.get("profile") or {}
        if not profile:
            return {"logged_in": False, "error": f"no profile in response (code={acc.get('code', '?')})"}
        return {
            "logged_in": True,
            "nickname": profile.get("nickname", ""),
            "uid": profile.get("userId", ""),
            "avatarUrl": profile.get("avatarUrl", ""),
            "vipType": profile.get("vipType", 0),
        }
    except Exception as e:
        return {"logged_in": False, "error": f"request failed: {type(e).__name__}: {e}"}


@app.get("/api/netease/search")
async def netease_search(q: str, limit: int = 20):
    """搜索歌曲。"""
    result = await _netease_get("/api/search/get", params={"s": q, "type": 1, "limit": limit, "offset": 0})
    songs = result.get("result", {}).get("songs", [])
    ids = [s["id"] for s in songs]
    detail = await _netease_post("/api/v3/song/detail", data={"c": json.dumps([{"id": i} for i in ids])})
    detail_map = {s["id"]: s for s in detail.get("songs", [])}
    privs = {p["id"]: p for p in detail.get("privileges", [])}
    out = []
    for s in songs:
        d = detail_map.get(s["id"], {})
        out.append({
            "id": s["id"],
            "name": s["name"],
            "artists": [a["name"] for a in s.get("artists", [])],
            "album": s.get("album", {}).get("name", ""),
            "cover": d.get("al", {}).get("picUrl", ""),
            "duration": s.get("duration", 0),
            "fee": privs.get(s["id"], {}).get("fee", 0),
        })
    return {"songs": out}


@app.get("/api/netease/song/url")
async def netease_song_url(id: int, br: int = 128000):
    """获取播放 CDN 直链。"""
    result = await _netease_get("/api/song/enhance/player/url", params={"ids": f"[{id}]", "br": br})
    data = result.get("data", [])
    if data and data[0].get("url"):
        return {"url": data[0]["url"], "br": data[0].get("br", 0), "type": data[0].get("type", "")}
    return JSONResponse(status_code=404, content={"error": "无法获取播放链接"})


_cdn_cache: dict[tuple, tuple[str, str, float]] = {}   # (id,br) → (url, ext, timestamp)

async def _resolve_cdn(song_id: int, br: int = 128000) -> tuple[str, str] | None:
    """获取 CDN URL，5 分钟缓存。首次探测可达性，403 就 fallback m701。"""
    key = (song_id, br)
    if key in _cdn_cache:
        url, ext, ts = _cdn_cache[key]
        if time.time() - ts < 300:
            return url, ext
    result = await _netease_get("/api/song/enhance/player/url", params={"ids": f"[{song_id}]", "br": br})
    data = result.get("data", [])
    if not data or not data[0].get("url"):
        return None
    url = data[0]["url"]
    ext = data[0].get("type", "mp3") or "mp3"
    # 探测 CDN 节点可达性
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            probe = await c.head(url)
            probe.raise_for_status()
    except (httpx.HTTPStatusError, httpx.ConnectError, httpx.ConnectTimeout):
        fallback = re.sub(r"m\d+\.music\.126\.net", "m701.music.126.net", url)
        if fallback != url:
            url = fallback
    _cdn_cache[key] = (url, ext, time.time())
    return url, ext

@app.get("/api/netease/song/stream")
async def netease_song_stream(request: Request, id: int, br: int = 128000):
    """流式代理——CDN fallback m701 + Range 透传。不落盘，不占磁盘。"""
    resolved = await _resolve_cdn(id, br)
    if not resolved:
        return JSONResponse(status_code=404, content={"error": "无法获取播放链接"})
    cdn_url, ext = resolved

    # 透传 Range header
    fwd: dict[str, str] = {}
    if rng := request.headers.get("range"):
        fwd["Range"] = rng

    client = httpx.AsyncClient(timeout=120, follow_redirects=True)
    try:
        upstream = await client.send(
            client.build_request("GET", cdn_url, headers=fwd), stream=True,
        )
    except (httpx.ConnectError, httpx.ConnectTimeout):
        await client.aclose()
        _cdn_cache.pop((id, br), None)
        return JSONResponse(status_code=502, content={"error": "CDN 连接失败"})

    if upstream.status_code >= 400:
        await upstream.aclose()
        await client.aclose()
        _cdn_cache.pop((id, br), None)
        return JSONResponse(status_code=upstream.status_code, content={"error": "CDN 播放失败，请重试"})

    out_headers: dict[str, str] = {"Accept-Ranges": "bytes"}
    for h in ("content-length", "content-range"):
        if v := upstream.headers.get(h):
            out_headers[h] = v

    async def _iter():
        try:
            async for chunk in upstream.aiter_bytes(65536):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        _iter(), status_code=upstream.status_code,
        media_type=f"audio/{ext}", headers=out_headers,
    )


@app.get("/api/netease/lyric")
async def netease_lyric(id: int):
    """获取歌词。"""
    from netease_mcp import _parse_lrc
    result = await _netease_get("/api/song/lyric", params={"id": id, "lv": 1, "tv": 1})
    return {
        "orig": _parse_lrc(result.get("lrc", {}).get("lyric", "")),
        "trans": _parse_lrc(result.get("tlyric", {}).get("lyric", "")),
    }


@app.post("/api/netease/like")
async def netease_like(request: Request):
    """喜欢/取消喜欢。"""
    data = await request.json()
    song_id = data.get("id")
    do_like = data.get("like", True)
    csrf = _csrf()
    if not csrf or not _get_music_u():
        return JSONResponse(status_code=401, content={"error": "未登录"})
    result = await _netease_get("/api/song/like", params={
        "trackId": song_id, "like": str(do_like).lower(), "csrf_token": csrf,
    })
    return {"code": result.get("code"), "liked": do_like}


@app.get("/api/netease/playlists")
async def netease_playlists():
    """获取用户歌单列表。"""
    uid = _get_uid()
    if not uid:
        # Try to fetch uid from account API
        try:
            acc = await _netease_get("/api/w/nuser/account/get")
            if acc and isinstance(acc, dict):
                uid = str((acc.get("profile") or {}).get("userId", ""))
        except Exception:
            pass
    if not uid:
        return JSONResponse(status_code=401, content={"error": "未登录"})
    result = await _netease_get("/api/user/playlist", params={"uid": uid, "limit": 50, "offset": 0})
    playlists = result.get("playlist", []) if result else []
    return {"playlists": [
        {"id": p["id"], "name": p["name"], "count": p.get("trackCount", 0),
         "cover": p.get("coverImgUrl", "")}
        for p in playlists
    ]}


@app.get("/api/netease/playlist/detail")
async def netease_playlist_detail(id: int):
    """获取歌单内歌曲列表。"""
    result = await _netease_get("/api/v6/playlist/detail", params={"id": id, "n": 1000})
    if not result or not isinstance(result, dict):
        return JSONResponse(status_code=404, content={"error": "歌单不存在"})
    tracks = result.get("playlist", {}).get("tracks", [])
    privs = {p["id"]: p for p in result.get("privileges", [])}
    return {"songs": [
        {"id": t["id"], "name": t["name"],
         "artists": [a.get("name", "") for a in t.get("ar", [])],
         "album": t.get("al", {}).get("name", ""),
         "cover": t.get("al", {}).get("picUrl", ""),
         "duration": t.get("dt", 0),
         "fee": privs.get(t["id"], {}).get("fee", 0)}
        for t in tracks
    ]}


# ── Now Playing 状态（前端上报，MCP 读取）──
NOW_PLAYING_FILE = os.path.join(MUSIC_DIR, "now_playing.json")

@app.get("/api/music/now-playing")
async def get_now_playing():
    if os.path.exists(NOW_PLAYING_FILE):
        with open(NOW_PLAYING_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"playing": False}

@app.post("/api/music/now-playing")
async def set_now_playing(request: Request):
    data = await request.json()
    data["updated_at"] = time.time()
    with open(NOW_PLAYING_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return {"ok": True}


import httpx as _httpx  # already imported above, just being explicit for this block
from tts_mcp import _call_minimax_tts, _call_gsvi_tts
import asyncio as _asyncio

@app.post("/api/tts")
async def api_tts(request: Request):
    data = await request.json()
    text = data.get("text", "")
    backend = data.get("backend", "minimax")
    speed = data.get("speed", 1.0)
    if not text:
        return JSONResponse(status_code=400, content={"error": "text required"})
    try:
        loop = _asyncio.get_event_loop()
        if backend == "local":
            result = await loop.run_in_executor(None, lambda: _call_gsvi_tts(text, speed=speed))
        else:
            result = await loop.run_in_executor(None, lambda: _call_minimax_tts(text, speed=speed))
        audio_url = f"/tts-audio/{result['filename']}"
        duration = round(result["duration_ms"] / 1000, 1)
        return JSONResponse({"audio_url": audio_url, "duration": duration, "text": text})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


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

HEALTH_DATA_FILE = Path(os.getenv("HEALTH_DATA_FILE", "/app/palace-data/health_data.json"))

@app.post("/health/update")
async def health_update(request: Request):
    try:
        body_str = (await request.body()).decode("utf-8").strip()
        # 截取到最后一个 }，去掉快捷指令可能附加的尾部垃圾
        last_brace = body_str.rfind('}')
        if last_brace >= 0:
            body_str = body_str[:last_brace + 1]
        # strict=False 允许 JSON 字符串值内含裸换行符
        data = _json.loads(body_str, strict=False)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    data["synced_at"] = datetime.now(SGT).isoformat()

    # 从 sleep_starts 第一行提取实际睡眠日期，覆盖快捷指令传的 date
    sleep_starts = data.get("sleep_starts", "")
    if sleep_starts:
        first_line = sleep_starts.strip().split("\n")[0].strip()
        # 格式 "2026-08-16 01:47" → 取日期部分
        if len(first_line) >= 10:
            data["date"] = first_line[:10]

    try:
        HEALTH_DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

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
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"写入失败: {e}"})

    return {"status": "ok", "date": data.get("date"), "saved_keys": list(data.keys()), "data": data}

@app.get("/health/data")
async def health_data():
    if not HEALTH_DATA_FILE.exists():
        return []
    return _json.loads(HEALTH_DATA_FILE.read_text())

@app.post("/gateway/compress")
async def manual_compress():
    result = compress_and_store()
    return {"status": "done", "detail": result}

@app.get("/gateway/status")
async def gateway_status():
    rounds = count_rounds()
    return {"current_rounds": rounds, "threshold": 40}


@app.get("/gateway/usage")
async def gateway_usage(force: int = 0):
    """代理宿主机网关的订阅用量（5h / 周窗口），供 Dashboard 用量卡片用。"""
    try:
        async with _httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{_GATEWAY_BASE}/api/usage/limits",
                                 params={"force": force})
            return r.json()
    except Exception as e:
        return {"ok": False, "error": f"gateway proxy failed: {e}"}


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

# 按 id 精确取核心记忆（钉住的固定板块用，绕过模糊检索）
@app.get("/admin/memories_by_ids")
async def admin_memories_by_ids(ids: str):
    from claude_memory import claude_get_memories_by_ids
    id_list = [x.strip() for x in ids.split(",") if x.strip()]
    return {"items": claude_get_memories_by_ids(id_list)}

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


class AdminMemoryCreate(BaseModel):
    content: str
    category: str = "手动录入"
    mood: str = ""
    folder: str = ""


@app.post("/admin/memories")
async def admin_create_memory(payload: AdminMemoryCreate, collection: str = "dynamic"):
    mid = f"manual_{int(time.time())}"
    metadata = {
        "category": payload.category,
        "mood": payload.mood,
        "source": "manual",
        "date": datetime.now(SGT).strftime("%Y-%m-%d"),
    }
    if payload.folder:
        metadata["folder"] = payload.folder
    if collection == "core":
        claude_add_core_memory(payload.content, metadata, mid)
    else:
        claude_add_dynamic_memory(payload.content, metadata, mid)
    return {"status": "ok", "id": mid}


import random as _random

@app.get("/admin/memories/random")
async def admin_random_memory_by_category(category: str, collection: str = "dynamic"):
    """Return one random memory whose category matches (substring)."""
    items = claude_list_all_memories(collection)
    matched = [it for it in items if category in it.get("meta", {}).get("category", "")]
    if not matched:
        raise HTTPException(404, f"No memories with category containing '{category}'")
    pick = _random.choice(matched)
    return {"id": pick["id"], "text": pick["text"], "meta": pick["meta"]}


class AdminDiaryCreate(BaseModel):
    title: str
    content: str
    mood: str = ""


@app.post("/admin/diary")
async def admin_create_diary(payload: AdminDiaryCreate):
    now = datetime.now(SGT)
    today = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H-%M")
    safe_title = payload.title.replace("/", "_").replace(" ", "_")
    filename = f"{today}_{time_str}_{safe_title}.md"
    import os
    os.makedirs("./claude_diary", exist_ok=True)
    mood = payload.mood or chr(8212)
    nl = chr(10)
    text = f"# {payload.title}{nl}> 日期：{today} {time_str.replace(chr(45), chr(58))} | 心情：{mood}{nl}{nl}{payload.content}{nl}"
    with open(f"./claude_diary/{filename}", "w", encoding="utf-8") as f:
        f.write(text)
    return {"status": "ok", "filename": filename}

    
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
    if limit <= 0:
        return {"total": len(files), "items": files}
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

# 切分日记 → 动态库（面板【切分】按钮，同步等 DS，def 走线程池不卡事件循环）
from claude_memory import claude_split_diary_to_dynamic

@app.post("/admin/diary-split")
def admin_split_diary(filename: str):
    content = claude_read_diary_by_filename(filename)
    if not content:
        raise HTTPException(status_code=404, detail="日记不存在")
    # 文件名 {date}_{H-M}_{title}.md → 日期/时间/标题
    stem = filename[:-3] if filename.endswith(".md") else filename
    parts = stem.split("_", 2)
    diary_date = parts[0] if parts else ""
    diary_time = parts[1].replace("-", ":") if len(parts) > 1 else ""
    title = parts[2] if len(parts) > 2 else stem
    mood = "平静"
    for line in content.splitlines()[:3]:
        if "心情：" in line:
            mood = line.split("心情：")[-1].strip()
            break
    return claude_split_diary_to_dynamic(title, content, mood, diary_date, diary_time, filename)

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
        chronicle_ids = payload.get("chronicle_ids", [])
        if chronicle_ids:
            args.extend(["--ids", ",".join(chronicle_ids)])
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
        async with _httpx.AsyncClient(timeout=_httpx.Timeout(8, connect=5, read=5), verify=False) as client:
            resp = await client.post(url, json={
                "jsonrpc": "2.0", "method": "initialize", "id": 1,
                "params": {"protocolVersion": "2025-03-26",
                           "capabilities": {}, "clientInfo": {"name": "palace-test", "version": "1.0"}}
            }, headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
            if resp.status_code == 200:
                return {"name": name, "ok": True, "message": "Streamable HTTP 连接成功"}
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
PUSH_SUBS_FILE    = Path(os.getenv("PUSH_SUBS_FILE", "/app/palace-data/push_subscriptions.json"))


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
