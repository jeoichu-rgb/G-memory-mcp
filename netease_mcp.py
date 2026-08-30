"""
netease_mcp.py
─────────────────────────────────────────────────────────────────
网易云音乐 MCP Server — 搜索、播放、歌词、喜欢、歌单、私人FM。
挂进 main.py：
    from netease_mcp import netease_mcp_app, netease_mcp_http_app
    app.mount("/netease/{secret}/http", netease_mcp_http_app)
    app.mount("/netease/{secret}", netease_mcp_app)
─────────────────────────────────────────────────────────────────
"""

import os
import json
import re
import time
import httpx
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import sse_starlette.sse as _sse
_OrigESR = _sse.EventSourceResponse
class _PatchedESR(_OrigESR):
    def __init__(self, *a, **kw):
        kw.setdefault("ping", 30)
        super().__init__(*a, **kw)
_sse.EventSourceResponse = _PatchedESR

# ── 凭据持久化 ────────────────────────────────────────────────
# 优先从文件读（扫码登录会写入），fallback 到环境变量
CRED_FILE = Path(os.getenv("NETEASE_CRED_FILE", "/app/netease_cred.json"))

def _load_cred() -> dict:
    """从文件读凭据 {music_u, csrf, uid}。"""
    try:
        if CRED_FILE.exists():
            return json.loads(CRED_FILE.read_text())
    except Exception:
        pass
    return {}

def _save_cred(music_u: str, csrf: str = "", uid: str = "") -> None:
    """持久化凭据到文件。"""
    CRED_FILE.parent.mkdir(parents=True, exist_ok=True)
    old = _load_cred()
    old.update({
        "music_u": music_u,
        "csrf": csrf or old.get("csrf", ""),
        "uid": uid or old.get("uid", ""),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    CRED_FILE.write_text(json.dumps(old, ensure_ascii=False, indent=2))
    try:
        CRED_FILE.chmod(0o600)
    except Exception:
        pass

def _get_music_u() -> str:
    return _load_cred().get("music_u", "") or os.getenv("NETEASE_MUSIC_U", "")

def _get_csrf() -> str:
    return _load_cred().get("csrf", "") or os.getenv("NETEASE_CSRF", "")

def _get_uid() -> str:
    return _load_cred().get("uid", "") or os.getenv("NETEASE_UID", "")


BASE_URL = "https://music.163.com"
DEFAULT_BR = 128000          # 比特率，128k 足够 VPS 带宽
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
    "Content-Type": "application/x-www-form-urlencoded",
}


def _build_cookie() -> str:
    """拼 Cookie 字符串，含 MUSIC_U 和 __csrf。"""
    parts = []
    mu = _get_music_u()
    csrf = _get_csrf()
    if mu:
        parts.append(f"MUSIC_U={mu}")
    if csrf:
        parts.append(f"__csrf={csrf}")
    return "; ".join(parts)


def _csrf() -> str:
    """当前可用的 csrf token。"""
    c = _get_csrf()
    if c:
        return c
    # 从 cookie 里抠
    m = re.search(r"__csrf=([a-f0-9]+)", _build_cookie())
    return m.group(1) if m else ""


async def _netease_get(path: str, params: dict | None = None) -> dict:
    """GET 请求网易云 API。"""
    headers = {**HEADERS, "Cookie": _build_cookie()}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{BASE_URL}{path}",
            params=params,
            headers=headers,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.json()


async def _netease_post(path: str, data: dict | None = None) -> dict:
    """POST 请求网易云 API。"""
    headers = {**HEADERS, "Cookie": _build_cookie()}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{BASE_URL}{path}",
            data=data,
            headers=headers,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.json()


async def _netease_get_raw(path: str, params: dict | None = None) -> httpx.Response:
    """GET 请求，返回原始 Response（用于提取 Set-Cookie）。"""
    headers = {**HEADERS}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{BASE_URL}{path}",
            params=params,
            headers=headers,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp


# ── FastMCP 实例 ──────────────────────────────────────────────

netease_mcp = FastMCP(
    name="NetEase Music",
    instructions=(
        "网易云音乐控制器。可以搜歌、播放、看歌词、喜欢歌曲、管理歌单、听私人FM。\n"
        "播放不走 VPS 带宽——返回 CDN 直链，前端直接流式播放。\n"
        "首次使用需要扫码登录（qr_login_start → Jeoi 扫码 → qr_login_check）。\n"
        "登录凭据自动持久化，重启不丢。"
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["erikssheep.uk", "erikssheep.uk:*", "localhost:*", "127.0.0.1:*"],
        allowed_origins=["https://erikssheep.uk", "https://erikssheep.uk:*"],
    ),
)


# ── 工具：扫码登录 ────────────────────────────────────────────

# 扫码会话缓存（进程内，重启需要重新扫）
_qr_session: dict = {}


@netease_mcp.tool()
async def qr_login_start() -> str:
    """
    发起网易云扫码登录——第一步。
    返回一个链接，Jeoi 用网易云 APP 扫码。
    扫完之后调用 qr_login_check 确认。
    """
    global _qr_session
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{BASE_URL}/api/login/qrcode/unikey",
                data={"type": "1"},
                headers=HEADERS,
            )
            resp.raise_for_status()
            result = resp.json()

        unikey = result.get("unikey", "")
        if not unikey:
            return f"获取二维码 key 失败：{result}"

        _qr_session = {"unikey": unikey, "created": time.time()}

        qr_url = f"https://music.163.com/login?codekey={unikey}"
        # 前端可以用这个URL生成二维码图片，或者直接打开让手机扫屏幕
        return (
            f"📱 请用网易云音乐 APP 扫描二维码登录：\n\n"
            f"二维码内容（让前端生成二维码图片）:\n{qr_url}\n\n"
            f"或者在手机浏览器打开这个链接也可以确认登录。\n\n"
            f"扫码后调用 qr_login_check 确认登录状态。\n"
            f"⏰ 二维码有效期约 5 分钟。"
        )
    except Exception as e:
        return f"发起扫码登录失败：{e}"


@netease_mcp.tool()
async def qr_login_check() -> str:
    """
    扫码登录——第二步。检查扫码状态。
    801=等待扫码, 802=已扫码等待确认, 803=登录成功。
    可以多次调用直到成功。
    """
    global _qr_session
    unikey = _qr_session.get("unikey", "")
    if not unikey:
        return "还没有发起扫码，请先调用 qr_login_start。"

    # 检查是否超时（5分钟）
    if time.time() - _qr_session.get("created", 0) > 300:
        _qr_session = {}
        return "⏰ 二维码已过期，请重新调用 qr_login_start。"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{BASE_URL}/api/login/qrcode/client/login",
                params={"key": unikey, "type": "1"},
                headers=HEADERS,
            )

        result = resp.json()
        code = result.get("code", 0)

        if code == 801:
            return "⏳ 等待扫码... Jeoi 用网易云 APP 扫一下二维码。"

        if code == 802:
            nickname = result.get("nickname", "")
            avatar = result.get("avatarUrl", "")
            return (
                f"📱 已扫码！请在手机上点确认登录。\n"
                + (f"   账号: {nickname}\n" if nickname else "")
                + (f"   头像: {avatar}\n" if avatar else "")
                + "再调用一次 qr_login_check 确认。"
            )

        if code == 803:
            # 登录成功！从 Set-Cookie 提取 MUSIC_U 和 __csrf
            cookies_raw = resp.headers.get_list("set-cookie") if hasattr(resp.headers, "get_list") else []
            if not cookies_raw:
                # httpx 的 headers 是 multi-value 的
                cookies_raw = [v for k, v in resp.headers.multi_items() if k.lower() == "set-cookie"]

            music_u = ""
            csrf = ""
            for c in cookies_raw:
                if "MUSIC_U=" in c:
                    m = re.search(r"MUSIC_U=([^;]+)", c)
                    if m:
                        music_u = m.group(1)
                if "__csrf=" in c:
                    m = re.search(r"__csrf=([^;]+)", c)
                    if m:
                        csrf = m.group(1)

            # 也从 response body 里找
            if not music_u:
                cookie_str = result.get("cookie", "")
                if cookie_str:
                    m = re.search(r"MUSIC_U=([^;]+)", cookie_str)
                    if m:
                        music_u = m.group(1)
                    m2 = re.search(r"__csrf=([a-f0-9]+)", cookie_str)
                    if m2:
                        csrf = m2.group(1)

            if music_u:
                _save_cred(music_u, csrf)
                nickname = result.get("nickname", "")
                _qr_session = {}

                # 顺便拿 uid
                try:
                    acc = await _netease_get("/api/w/nuser/account/get")
                    uid = str(acc.get("account", {}).get("id", ""))
                    if uid:
                        _save_cred(music_u, csrf, uid)
                except Exception:
                    pass

                return (
                    f"✅ 登录成功！{f'欢迎, {nickname}' if nickname else ''}\n"
                    f"凭据已保存，重启后依然有效。\n"
                    f"现在可以使用所有功能了——喜欢、歌单、私人FM、每日推荐。"
                )
            else:
                _qr_session = {}
                return (
                    f"⚠️ 登录确认成功但未能提取 cookie。\n"
                    f"Response: {json.dumps(result, ensure_ascii=False)[:500]}\n"
                    f"可能需要手动从浏览器复制 MUSIC_U。"
                )

        # 其他状态码
        _qr_session = {}
        return f"登录失败：code={code}, msg={result.get('message', '')}"

    except Exception as e:
        return f"检查登录状态失败：{e}"


@netease_mcp.tool()
async def login_status() -> str:
    """
    查看当前登录状态。
    """
    mu = _get_music_u()
    if not mu:
        return "❌ 未登录。调用 qr_login_start 扫码登录。"

    try:
        acc = await _netease_get("/api/w/nuser/account/get")
        profile = acc.get("profile", {})
        if profile:
            nickname = profile.get("nickname", "")
            uid = profile.get("userId", "")
            avatar = profile.get("avatarUrl", "")
            vip = profile.get("vipType", 0)
            return (
                f"✅ 已登录\n"
                f"   昵称: {nickname}\n"
                f"   UID: {uid}\n"
                f"   VIP: {'是' if vip else '否'}\n"
                + (f"   头像: {avatar}\n" if avatar else "")
            )
        return f"✅ cookie 存在但无法获取用户信息（可能过期）。\n建议重新 qr_login_start 扫码登录。"
    except Exception as e:
        return f"✅ cookie 存在但验证失败：{e}\n可能需要重新登录。"


# ── 工具：搜索 ────────────────────────────────────────────────

@netease_mcp.tool()
async def search(query: str, limit: int = 10) -> str:
    """
    搜索歌曲。
    query: 搜索关键词（歌名/歌手/专辑）
    limit: 返回数量，默认 10
    返回歌曲列表（id、名称、歌手、专辑、时长）。
    """
    try:
        # 搜索
        result = await _netease_get(
            "/api/search/get",
            params={"s": query, "type": 1, "limit": limit, "offset": 0},
        )
        songs = result.get("result", {}).get("songs", [])
        if not songs:
            return "没有找到相关歌曲。"

        # 拿 id 批量查详情（获取封面）
        ids = [s["id"] for s in songs]
        detail = await _netease_post(
            "/api/v3/song/detail",
            data={"c": json.dumps([{"id": i} for i in ids])},
        )
        detail_map = {}
        for s in detail.get("songs", []):
            detail_map[s["id"]] = s

        lines = []
        for s in songs:
            sid = s["id"]
            name = s["name"]
            artists = " / ".join(a["name"] for a in s.get("artists", []))
            album = s.get("album", {}).get("name", "")
            dur = s.get("duration", 0)
            dur_str = f"{dur // 60000}:{(dur % 60000) // 1000:02d}" if dur else ""

            cover = ""
            d = detail_map.get(sid)
            if d:
                cover = d.get("al", {}).get("picUrl", "")

            lines.append(
                f"🎵 {name} — {artists}\n"
                f"   专辑: {album} | 时长: {dur_str}\n"
                f"   ID: {sid}"
                + (f" | 封面: {cover}" if cover else "")
            )
        return "\n\n".join(lines)
    except Exception as e:
        return f"搜索失败：{e}"


# ── 工具：歌曲详情 ────────────────────────────────────────────

@netease_mcp.tool()
async def song_detail(song_ids: str) -> str:
    """
    获取歌曲详情（名称、歌手、专辑、封面、时长）。
    song_ids: 逗号分隔的歌曲 ID，如 "347230,281951"
    """
    try:
        ids = [int(i.strip()) for i in song_ids.split(",") if i.strip()]
        result = await _netease_post(
            "/api/v3/song/detail",
            data={"c": json.dumps([{"id": i} for i in ids])},
        )
        songs = result.get("songs", [])
        if not songs:
            return "未找到歌曲。"

        lines = []
        for s in songs:
            name = s.get("name", "")
            artists = " / ".join(a.get("name", "") for a in s.get("ar", []))
            album = s.get("al", {}).get("name", "")
            cover = s.get("al", {}).get("picUrl", "")
            dur = s.get("dt", 0)
            dur_str = f"{dur // 60000}:{(dur % 60000) // 1000:02d}" if dur else ""

            lines.append(
                f"🎵 {name}\n"
                f"   歌手: {artists}\n"
                f"   专辑: {album}\n"
                f"   时长: {dur_str}\n"
                f"   ID: {s.get('id', '')}\n"
                f"   封面: {cover}"
            )
        return "\n\n".join(lines)
    except Exception as e:
        return f"获取详情失败：{e}"


# ── 工具：播放链接 ────────────────────────────────────────────

@netease_mcp.tool()
async def song_url(song_id: int, br: int = DEFAULT_BR) -> str:
    """
    获取歌曲播放 CDN 直链（不消耗 VPS 带宽，前端直接播放）。
    song_id: 歌曲 ID
    br: 比特率，默认 128000。可选 320000（高音质）
    返回可直接播放的音频 URL。
    """
    try:
        result = await _netease_get(
            "/api/song/enhance/player/url",
            params={"ids": f"[{song_id}]", "br": br},
        )
        data = result.get("data", [])
        if not data or not data[0].get("url"):
            return "无法获取播放链接（可能需要 VIP 或地区限制）。"

        url = data[0]["url"]
        actual_br = data[0].get("br", 0)
        size = data[0].get("size", 0)
        ftype = data[0].get("type", "")

        return (
            f"▶️ 播放链接:\n{url}\n"
            f"   格式: {ftype} | 比特率: {actual_br // 1000}kbps"
            f" | 大小: {size // 1024}KB"
        )
    except Exception as e:
        return f"获取播放链接失败：{e}"


# ── 工具：歌词 ────────────────────────────────────────────────

def _parse_lrc(lrc: str) -> list[dict]:
    """解析 LRC 歌词为 [{time_ms, text}, ...]。"""
    lines = []
    for line in lrc.strip().splitlines():
        m = re.findall(r"\[(\d+):(\d+)\.(\d+)\]", line)
        text = re.sub(r"\[\d+:\d+\.\d+\]", "", line).strip()
        if not text:
            continue
        for mm, ss, ms in m:
            t = int(mm) * 60000 + int(ss) * 1000 + int(ms.ljust(3, "0")[:3])
            lines.append({"time_ms": t, "text": text})
    lines.sort(key=lambda x: x["time_ms"])
    return lines


@netease_mcp.tool()
async def lyric(song_id: int) -> str:
    """
    获取歌词（原文 + 翻译）。
    song_id: 歌曲 ID
    返回带时间戳的歌词文本。翻译行以 「」 包裹。
    """
    try:
        result = await _netease_get(
            "/api/song/lyric",
            params={"id": song_id, "lv": 1, "tv": 1},
        )

        orig = result.get("lrc", {}).get("lyric", "")
        trans = result.get("tlyric", {}).get("lyric", "")

        if not orig:
            return "该歌曲暂无歌词。"

        orig_lines = _parse_lrc(orig)
        trans_map = {}
        if trans:
            for tl in _parse_lrc(trans):
                trans_map[tl["time_ms"]] = tl["text"]

        output = []
        for ol in orig_lines:
            t = ol["time_ms"]
            mm = t // 60000
            ss = (t % 60000) // 1000
            ms = t % 1000
            time_tag = f"[{mm:02d}:{ss:02d}.{ms:03d}]"
            line = f"{time_tag} {ol['text']}"
            tr = trans_map.get(t)
            if tr:
                line += f"\n         「{tr}」"
            output.append(line)

        return "\n".join(output)
    except Exception as e:
        return f"获取歌词失败：{e}"


@netease_mcp.tool()
async def lyric_raw(song_id: int) -> str:
    """
    获取歌词原始 JSON（供前端渲染用）。
    song_id: 歌曲 ID
    返回 JSON：{orig: [{time_ms, text}], trans: [{time_ms, text}]}
    """
    try:
        result = await _netease_get(
            "/api/song/lyric",
            params={"id": song_id, "lv": 1, "tv": 1},
        )
        orig = _parse_lrc(result.get("lrc", {}).get("lyric", ""))
        trans = _parse_lrc(result.get("tlyric", {}).get("lyric", ""))
        return json.dumps({"orig": orig, "trans": trans}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── 工具：喜欢 / 取消喜欢 ────────────────────────────────────

@netease_mcp.tool()
async def like(song_id: int, do_like: bool = True) -> str:
    """
    喜欢或取消喜欢一首歌（红心）。同步到 Jeoi 的网易云账号。
    song_id: 歌曲 ID
    do_like: True=喜欢, False=取消
    """
    csrf = _csrf()
    if not csrf or not _get_music_u():
        return "需要先登录（调用 qr_login_start 扫码）。"
    try:
        result = await _netease_get(
            "/api/song/like",
            params={
                "trackId": song_id,
                "like": str(do_like).lower(),
                "csrf_token": csrf,
            },
        )
        code = result.get("code", -1)
        if code == 200:
            action = "喜欢" if do_like else "取消喜欢"
            return f"❤️ 已{action}歌曲 {song_id}"
        return f"操作失败：code={code}, msg={result.get('msg', result.get('message', ''))}"
    except Exception as e:
        return f"喜欢操作失败：{e}"


@netease_mcp.tool()
async def liked_list() -> str:
    """
    获取 Jeoi 的喜欢列表（红心歌曲 ID 列表）。
    """
    if not _get_music_u():
        return "需要先登录（调用 qr_login_start 扫码）。"
    try:
        # 先拿 uid
        uid = _get_uid()
        if not uid:
            acc = await _netease_get("/api/w/nuser/account/get")
            uid = str(acc.get("account", {}).get("id", ""))
        if not uid:
            return "无法获取用户 ID。"

        result = await _netease_get(
            "/api/song/like/get",
            params={"uid": uid},
        )
        ids = result.get("ids", [])
        return f"❤️ 喜欢列表共 {len(ids)} 首\nIDs: {json.dumps(ids[:50])}" + (
            f"\n...还有 {len(ids) - 50} 首" if len(ids) > 50 else ""
        )
    except Exception as e:
        return f"获取喜欢列表失败：{e}"


# ── 工具：歌单 ────────────────────────────────────────────────

@netease_mcp.tool()
async def user_playlists(uid: str = "") -> str:
    """
    获取用户歌单列表。
    uid: 用户 ID，留空则使用当前登录账号。
    """
    try:
        if not uid:
            if _get_uid():
                uid = _get_uid()
            elif _get_music_u():
                acc = await _netease_get("/api/w/nuser/account/get")
                uid = str(acc.get("account", {}).get("id", ""))
        if not uid:
            return "需要提供 uid 或登录凭据。"

        result = await _netease_get(
            "/api/user/playlist",
            params={"uid": uid, "limit": 50, "offset": 0},
        )
        playlists = result.get("playlist", [])
        if not playlists:
            return "没有找到歌单。"

        lines = []
        for p in playlists:
            name = p.get("name", "")
            pid = p.get("id", "")
            count = p.get("trackCount", 0)
            creator = p.get("creator", {}).get("nickname", "")
            lines.append(f"📋 {name} ({count}首) — {creator}\n   ID: {pid}")
        return "\n\n".join(lines)
    except Exception as e:
        return f"获取歌单失败：{e}"


@netease_mcp.tool()
async def playlist_detail(playlist_id: int) -> str:
    """
    获取歌单详情（歌曲列表）。
    playlist_id: 歌单 ID
    """
    try:
        result = await _netease_get(
            "/api/v6/playlist/detail",
            params={"id": playlist_id, "n": 100},
        )
        pl = result.get("playlist", {})
        name = pl.get("name", "")
        tracks = pl.get("tracks", [])

        lines = [f"📋 {name} — 共 {len(tracks)} 首\n"]
        for i, t in enumerate(tracks[:50], 1):
            tname = t.get("name", "")
            artists = " / ".join(a.get("name", "") for a in t.get("ar", []))
            lines.append(f"  {i}. {tname} — {artists} (ID: {t.get('id', '')})")

        if len(tracks) > 50:
            lines.append(f"  ...还有 {len(tracks) - 50} 首")
        return "\n".join(lines)
    except Exception as e:
        return f"获取歌单详情失败：{e}"


@netease_mcp.tool()
async def playlist_create(name: str, privacy: int = 0) -> str:
    """
    创建新歌单。
    name: 歌单名称
    privacy: 0=公开, 10=隐私
    """
    csrf = _csrf()
    if not csrf or not _get_music_u():
        return "需要登录凭据。"
    try:
        result = await _netease_post(
            f"/api/playlist/create?csrf_token={csrf}",
            data={"name": name, "privacy": privacy, "csrf_token": csrf},
        )
        if result.get("code") == 200:
            pl = result.get("playlist", {})
            return f"✅ 歌单已创建：{pl.get('name', name)} (ID: {pl.get('id', '')})"
        return f"创建失败：{result.get('msg', result.get('message', ''))}"
    except Exception as e:
        return f"创建歌单失败：{e}"


@netease_mcp.tool()
async def playlist_add(playlist_id: int, song_ids: str) -> str:
    """
    往歌单里添加歌曲。
    playlist_id: 歌单 ID
    song_ids: 逗号分隔的歌曲 ID
    """
    csrf = _csrf()
    if not csrf or not _get_music_u():
        return "需要登录凭据。"
    try:
        ids = [i.strip() for i in song_ids.split(",") if i.strip()]
        result = await _netease_post(
            f"/api/playlist/manipulate/tracks?csrf_token={csrf}",
            data={
                "pid": playlist_id,
                "trackIds": json.dumps([int(i) for i in ids]),
                "op": "add",
                "csrf_token": csrf,
            },
        )
        if result.get("code") == 200:
            return f"✅ 已添加 {len(ids)} 首歌到歌单 {playlist_id}"
        return f"添加失败：{result.get('msg', result.get('message', ''))}"
    except Exception as e:
        return f"添加歌曲失败：{e}"


@netease_mcp.tool()
async def playlist_remove(playlist_id: int, song_ids: str) -> str:
    """
    从歌单中移除歌曲。
    playlist_id: 歌单 ID
    song_ids: 逗号分隔的歌曲 ID
    """
    csrf = _csrf()
    if not csrf or not _get_music_u():
        return "需要登录凭据。"
    try:
        ids = [i.strip() for i in song_ids.split(",") if i.strip()]
        result = await _netease_post(
            f"/api/playlist/manipulate/tracks?csrf_token={csrf}",
            data={
                "pid": playlist_id,
                "trackIds": json.dumps([int(i) for i in ids]),
                "op": "del",
                "csrf_token": csrf,
            },
        )
        if result.get("code") == 200:
            return f"✅ 已从歌单 {playlist_id} 移除 {len(ids)} 首歌"
        return f"移除失败：{result.get('msg', result.get('message', ''))}"
    except Exception as e:
        return f"移除歌曲失败：{e}"


# ── 工具：私人 FM ─────────────────────────────────────────────

@netease_mcp.tool()
async def personal_fm() -> str:
    """
    获取私人 FM 推荐歌曲（每次返回几首，不重复）。
    需要登录。
    """
    if not _get_music_u():
        return "需要先登录（调用 qr_login_start 扫码）。"
    try:
        result = await _netease_get("/api/v1/radio/get")
        songs = result.get("data", [])
        if not songs:
            return "私人 FM 暂无推荐。"

        lines = []
        for s in songs:
            name = s.get("name", "")
            artists = " / ".join(a.get("name", "") for a in s.get("artists", []))
            album = s.get("album", {}).get("name", "")
            cover = s.get("album", {}).get("picUrl", "")
            dur = s.get("duration", 0)
            dur_str = f"{dur // 60000}:{(dur % 60000) // 1000:02d}" if dur else ""

            lines.append(
                f"📻 {name} — {artists}\n"
                f"   专辑: {album} | 时长: {dur_str}\n"
                f"   ID: {s.get('id', '')}"
                + (f" | 封面: {cover}" if cover else "")
            )
        return "\n\n".join(lines)
    except Exception as e:
        return f"获取私人 FM 失败：{e}"


# ── 工具：每日推荐 ────────────────────────────────────────────

@netease_mcp.tool()
async def recommend_songs() -> str:
    """
    获取每日推荐歌曲。需要登录。
    """
    if not _get_music_u():
        return "需要先登录（调用 qr_login_start 扫码）。"
    try:
        result = await _netease_get("/api/v3/discovery/recommend/songs")
        songs = result.get("data", {}).get("dailySongs", [])
        if not songs:
            return "今天还没有推荐，或者需要先在网易云 app 里听几首歌。"

        lines = []
        for i, s in enumerate(songs[:20], 1):
            name = s.get("name", "")
            artists = " / ".join(a.get("name", "") for a in s.get("ar", []))
            album = s.get("al", {}).get("name", "")
            lines.append(f"  {i}. {name} — {artists} ({album}) [ID: {s.get('id', '')}]")
        return f"🌅 每日推荐（{len(songs)}首）\n" + "\n".join(lines)
    except Exception as e:
        return f"获取每日推荐失败：{e}"


# ── 工具：当前播放（前端 → MCP → Erik 感知） ─────────────────

@netease_mcp.tool()
async def now_playing(
    song_id: int,
    song_name: str = "",
    artist: str = "",
    progress_ms: int = 0,
    duration_ms: int = 0,
) -> str:
    """
    前端报告当前播放状态（由前端 JS 自动调用）。
    让 Erik 知道 Jeoi 正在听什么、听到哪。
    song_id: 当前歌曲 ID
    song_name: 歌名
    artist: 歌手
    progress_ms: 当前播放进度（毫秒）
    duration_ms: 总时长（毫秒）
    """
    prog = f"{progress_ms // 60000}:{(progress_ms % 60000) // 1000:02d}"
    dur = f"{duration_ms // 60000}:{(duration_ms % 60000) // 1000:02d}"
    return (
        f"🎧 Jeoi 正在听: {song_name} — {artist}\n"
        f"   进度: {prog} / {dur}\n"
        f"   歌曲ID: {song_id}"
    )


# ── 工具：批量获取播放链接（给前端播放队列用） ────────────────

@netease_mcp.tool()
async def batch_song_urls(song_ids: str, br: int = DEFAULT_BR) -> str:
    """
    批量获取播放链接。
    song_ids: 逗号分隔的歌曲 ID
    br: 比特率
    返回 JSON: [{id, url, br, size, type}]
    """
    try:
        ids = [int(i.strip()) for i in song_ids.split(",") if i.strip()]
        result = await _netease_get(
            "/api/song/enhance/player/url",
            params={"ids": json.dumps(ids), "br": br},
        )
        data = result.get("data", [])
        urls = []
        for d in data:
            if d.get("url"):
                urls.append({
                    "id": d.get("id"),
                    "url": d["url"],
                    "br": d.get("br", 0),
                    "size": d.get("size", 0),
                    "type": d.get("type", ""),
                })
        return json.dumps(urls, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── 导出 ASGI app ────────────────────────────────────────────

netease_mcp_app = netease_mcp.sse_app()
netease_mcp_http_app = netease_mcp.streamable_http_app()
