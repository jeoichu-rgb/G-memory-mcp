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
import asyncio
import bisect
import logging
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
_HERE = Path(__file__).resolve().parent
CRED_FILE = Path(os.getenv("NETEASE_CRED_FILE", str(_HERE / "netease_cred.json")))

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

_spec_log = logging.getLogger("spectrum")
_spec_log.setLevel(logging.DEBUG)

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


# ══════════════════════════════════════════════════════════════
# 一起听 · Listen Together
# 基于 BB0813/netease-listen-together-mcp (MIT License) 的
# HTTP 信令逻辑，适配为 async httpx + FastMCP 风格。
# ══════════════════════════════════════════════════════════════

# ── 一起听：内部状态 ────────────────────────────────────────

# Jeoi 的网易云 UID（邀请/私信用）
ACCEPTOR_ID = os.getenv("NETEASE_LISTEN_TOGETHER_ACCEPTOR_ID", "")

# 心跳 / 状态监听 task handle
_lt_heartbeat_task: asyncio.Task | None = None
_lt_last_status: dict = {}            # 上一次轮询的播放状态快照
_lt_on_event: list = []               # 事件回调列表 (gateway 注册)


# ── 一起听：底层 HTTP API ───────────────────────────────────

async def _lt_post(path: str, extra: dict | None = None) -> dict:
    """一起听专用 POST，自动带 csrf_token。"""
    csrf = _csrf()
    url = f"{BASE_URL}{path}?csrf_token={csrf}"
    data = {"csrf_token": csrf}
    if extra:
        data.update(extra)
    return await _netease_post(url.replace(BASE_URL, ""), data)


async def _lt_get_status() -> dict:
    """获取当前一起听房间状态。"""
    csrf = _csrf()
    raw = await _netease_post(
        "/api/listen/together/status/get",
        data={"csrf_token": csrf},
    )
    data = raw.get("data") or {}
    room_info = data.get("roomInfo") or {}
    return {
        "inRoom": bool(data.get("inRoom")),
        "roomId": room_info.get("roomId"),
        "creatorId": room_info.get("creatorId"),
        "status": data.get("status"),
        "roomUsers": room_info.get("roomUsers") or [],
        "chatRoomId": room_info.get("chatRoomId"),
        "data": data,
    }


async def _lt_get_room_state(room_id: str) -> dict:
    """获取房间播放列表+当前播放指令。"""
    csrf = _csrf()
    res = await _netease_post(
        "/api/listen/together/sync/playlist/get",
        data={"roomId": str(room_id), "csrf_token": csrf},
    )
    if res.get("code") != 200:
        raise RuntimeError(f"sync/playlist/get failed: {res}")
    return res.get("data") or {}


async def _lt_report_play_command(room_id: str, command: dict) -> dict:
    """上报播放指令（切歌/暂停/继续/进度）。"""
    csrf = _csrf()
    res = await _netease_post(
        "/api/listen/together/play/command/report",
        data={
            "roomId": str(room_id),
            "commandInfo": json.dumps(command, ensure_ascii=False, separators=(",", ":")),
            "csrf_token": csrf,
        },
    )
    if res.get("code") != 200 or not (res.get("data") or {}).get("result"):
        raise RuntimeError(f"play/command/report failed: {res}")
    return res


async def _lt_report_list_command(room_id: str, command: dict) -> dict:
    """上报列表指令（加歌）。"""
    csrf = _csrf()
    res = await _netease_post(
        "/api/listen/together/sync/list/command/report",
        data={
            "roomId": str(room_id),
            "commandInfo": json.dumps(command, ensure_ascii=False, separators=(",", ":")),
            "csrf_token": csrf,
        },
    )
    if res.get("code") != 200 or not (res.get("data") or {}).get("result"):
        raise RuntimeError(f"sync/list/command/report failed: {res}")
    return res


async def _lt_send_chatroom_msg(chatroom_id: str, room_id: str, content: str) -> bool:
    """在一起听聊天室发消息。"""
    csrf = _csrf()
    res = await _netease_post(
        f"/api/middle/im/chatroom/send?csrf_token={csrf}",
        data={
            "chatroomId": str(chatroom_id),
            "msgType": "0",
            "clientExt": json.dumps({"bizType": "listenTogether", "roomId": str(room_id)}),
            "msgBody": json.dumps({"msg": content, "msgType": 0}),
            "csrf_token": csrf,
        },
    )
    return bool(res.get("code") == 200 and (res.get("data") or {}).get("result"))


async def _lt_accept_invitation(room_id: str, inviter_id: str) -> dict:
    """接受指定房间的一起听邀请。"""
    csrf = _csrf()
    res = await _netease_post(
        "/api/listen/together/play/invitation/accept",
        data={
            "roomId": str(room_id),
            "inviterId": str(inviter_id),
            "csrf_token": csrf,
        },
    )
    return res


async def _lt_accept_latest_from_pm() -> tuple[bool, str]:
    """从私信历史里找最新的一起听邀请卡片并自动接受。"""
    acceptor_id = ACCEPTOR_ID
    if not acceptor_id:
        return False, "NETEASE_LISTEN_TOGETHER_ACCEPTOR_ID 未配置"
    csrf = _csrf()
    res = await _netease_post(
        "/api/msg/private/history",
        data={"userId": str(acceptor_id), "limit": "10", "csrf_token": csrf},
    )
    import urllib.parse
    for m in res.get("msgs", []):
        try:
            msg_obj = json.loads(m.get("msg", "{}"))
            if msg_obj.get("type") == 23:
                native_url = (msg_obj.get("generalMsg") or {}).get("nativeUrl", "")
                # 先解析外层 URL，取出 url1 参数并解码
                decoded_url = urllib.parse.unquote(native_url)
                rid, iid = "", ""
                parsed = urllib.parse.urlparse(native_url)
                qs = urllib.parse.parse_qs(parsed.query)
                if "url1" in qs:
                    # url1 里嵌套了真正的 roomId 和 inviterId
                    inner_url = qs["url1"][0]
                    inner_parsed = urllib.parse.urlparse(inner_url)
                    inner_qs = urllib.parse.parse_qs(inner_parsed.query)
                    rid = inner_qs.get("roomId", [""])[0]
                    iid = inner_qs.get("inviterId", [""])[0]
                else:
                    # fallback：直接从外层或解码后的 URL 提取
                    if "roomId=" in decoded_url and "inviterId=" in decoded_url:
                        dec_parsed = urllib.parse.urlparse(decoded_url)
                        dec_qs = urllib.parse.parse_qs(dec_parsed.query)
                        rid = dec_qs.get("roomId", [""])[0]
                        iid = dec_qs.get("inviterId", [""])[0]
                    elif "roomId=" in native_url:
                        qs_direct = urllib.parse.parse_qs(parsed.query)
                        rid = qs_direct.get("roomId", [""])[0]
                        iid = qs_direct.get("inviterId", [""])[0]
                if rid and iid:
                    acc = await _lt_accept_invitation(rid, iid)
                    if acc.get("code") == 200:
                        return True, f"已接受房间 {rid} 的邀请（邀请人: {iid}）"
        except Exception:
            pass
    return False, "近期私信中未找到有效的一起听邀请卡片"


# ── 一起听：歌词定位 ───────────────────────────────────────

_lyric_cache: dict[int, list] = {}


async def _lt_get_lyric_timeline(song_id: int) -> list[tuple[float, str]]:
    """获取歌词时间轴 [(seconds, text), ...]，带缓存。"""
    if song_id in _lyric_cache:
        return _lyric_cache[song_id]
    try:
        result = await _netease_get(
            "/api/song/lyric",
            params={"id": song_id, "lv": 1, "tv": 1},
        )
        lrc = result.get("lrc", {}).get("lyric", "")
        trans = result.get("tlyric", {}).get("lyric", "")
        if not lrc:
            return []
        orig_lines = _parse_lrc(lrc)
        trans_map = {}
        if trans:
            for tl in _parse_lrc(trans):
                trans_map[tl["time_ms"]] = tl["text"]
        timeline = []
        for ol in orig_lines:
            t_sec = ol["time_ms"] / 1000.0
            text = ol["text"]
            tr = trans_map.get(ol["time_ms"])
            if tr:
                text += f"「{tr}」"
            timeline.append((t_sec, text))
        _lyric_cache[song_id] = timeline
        # 限制缓存大小
        if len(_lyric_cache) > 200:
            oldest = next(iter(_lyric_cache))
            del _lyric_cache[oldest]
        return timeline
    except Exception:
        return []


# ── 一起听：MCP 工具 ───────────────────────────────────────

@netease_mcp.tool()
async def listen_together_status() -> str:
    """
    查看当前一起听状态：是否在房间、房间 ID、参与者。
    """
    if not _get_music_u():
        return "需要先登录。"
    try:
        st = await _lt_get_status()
        if not st["inRoom"]:
            return (
                "🎧 当前未在一起听房间中。\n"
                "让 Jeoi 在网易云 APP 发起一起听并邀请我，"
                "然后调用 listen_together_accept 加入。"
            )
        users = st.get("roomUsers") or []
        user_lines = []
        for u in users:
            nick = u.get("nickname", "")
            uid = u.get("userId", "")
            user_lines.append(f"  · {nick} (UID: {uid})")
        # 拿当前播放
        try:
            state = await _lt_get_room_state(st["roomId"])
            play_cmd = state.get("playCommand") or {}
            song_id = play_cmd.get("targetSongId")
            progress = int(play_cmd.get("progress") or 0)
            play_status = play_cmd.get("playStatus", "UNKNOWN")
            prog_str = f"{progress // 60000}:{(progress % 60000) // 1000:02d}"
            song_info = f"歌曲ID: {song_id} | 进度: {prog_str} | {play_status}"
        except Exception:
            song_info = "（无法获取播放状态）"
        return (
            f"🎧 一起听 · 房间 {st['roomId']}\n"
            f"参与者:\n" + "\n".join(user_lines) + "\n"
            f"当前播放: {song_info}"
        )
    except Exception as e:
        return f"获取状态失败：{e}"


@netease_mcp.tool()
async def listen_together_accept(room_id: str = "", inviter_id: str = "") -> str:
    """
    接受一起听邀请并加入房间。
    room_id + inviter_id: 指定房间和邀请人（从邀请链接提取）。
    都留空则自动从 Jeoi 的最新私信中找邀请卡片。
    """
    if not _get_music_u():
        return "需要先登录。"
    try:
        if room_id and inviter_id:
            res = await _lt_accept_invitation(room_id, inviter_id)
            if res.get("code") == 200:
                return f"✅ 已加入一起听房间 {room_id}！"
            return f"加入失败：{res.get('message', res)}"
        ok, msg = await _lt_accept_latest_from_pm()
        if ok:
            return f"✅ {msg}"
        return f"❌ {msg}"
    except Exception as e:
        return f"接受邀请失败：{e}"


@netease_mcp.tool()
async def listen_together_leave() -> str:
    """
    退出当前一起听房间。
    """
    if not _get_music_u():
        return "需要先登录。"
    try:
        st = await _lt_get_status()
        if not st["inRoom"] or not st["roomId"]:
            return "当前不在一起听房间中。"
        csrf = _csrf()
        res = await _netease_post(
            "/api/listen/together/end/v2",
            data={
                "roomId": st["roomId"],
                "shareInfo": "{}",
                "csrf_token": csrf,
            },
        )
        if res.get("code") == 200:
            return f"✅ 已退出一起听房间 {st['roomId']}"
        return f"退出失败：{res}"
    except Exception as e:
        return f"退出房间失败：{e}"


@netease_mcp.tool()
async def listen_together_context() -> str:
    """
    获取当前一起听的播放上下文：
    歌曲信息 + 当前歌词位置 + 附近 6 句歌词。
    """
    if not _get_music_u():
        return "需要先登录。"
    try:
        st = await _lt_get_status()
        if not st["inRoom"] or not st["roomId"]:
            return "当前不在一起听房间中。"
        state = await _lt_get_room_state(st["roomId"])
        play_cmd = state.get("playCommand") or {}
        song_id = play_cmd.get("targetSongId")
        if not song_id:
            return "房间内暂无播放。"
        # 歌曲详情
        detail = await _netease_post(
            "/api/v3/song/detail",
            data={"c": json.dumps([{"id": int(song_id)}])},
        )
        tracks = detail.get("songs") or []
        title = artist = album = ""
        duration_ms = 0
        if tracks:
            t = tracks[0]
            title = t.get("name", "")
            artist = " / ".join(a.get("name", "") for a in t.get("ar", []))
            album = t.get("al", {}).get("name", "")
            duration_ms = t.get("dt", 0)
        progress_ms = int(play_cmd.get("progress") or 0)
        play_status = play_cmd.get("playStatus", "UNKNOWN")
        elapsed = progress_ms / 1000.0
        # 歌词定位
        timeline = await _lt_get_lyric_timeline(int(song_id))
        lyrics_block = ""
        if timeline:
            cur_idx = max(0, bisect.bisect_right(
                [item[0] for item in timeline], elapsed
            ) - 1)
            lines = []
            for i in range(max(0, cur_idx - 1), min(len(timeline), cur_idx + 5)):
                sec, text = timeline[i]
                marker = " ◀" if i == cur_idx else ""
                mm = int(sec) // 60
                ss = int(sec) % 60
                lines.append(f"  [{mm:02d}:{ss:02d}] {text}{marker}")
            lyrics_block = "\n".join(lines)
        prog = f"{progress_ms // 60000}:{(progress_ms % 60000) // 1000:02d}"
        dur = f"{duration_ms // 60000}:{(duration_ms % 60000) // 1000:02d}"
        status_emoji = "▶️" if play_status == "PLAY" else "⏸️"
        return (
            f"{status_emoji} {title} — {artist}\n"
            f"   专辑: {album}\n"
            f"   进度: {prog} / {dur} | ID: {song_id}\n"
            + (f"\n歌词:\n{lyrics_block}" if lyrics_block else "\n（无歌词）")
        )
    except Exception as e:
        return f"获取播放上下文失败：{e}"


@netease_mcp.tool()
async def listen_together_control(command: str) -> str:
    """
    一起听播放控制。
    command: play / pause / next / previous
    """
    if not _get_music_u():
        return "需要先登录。"
    cmd = command.strip().upper()
    if cmd not in ("PLAY", "RESUME", "PAUSE", "NEXT", "PREVIOUS", "PREV"):
        return f"不支持的指令: {command}。可用: play / pause / next / previous"
    if cmd == "RESUME":
        cmd = "PLAY"
    if cmd == "PREV":
        cmd = "PREVIOUS"
    try:
        st = await _lt_get_status()
        if not st["inRoom"] or not st["roomId"]:
            return "当前不在一起听房间中。"
        room_id = st["roomId"]
        state = await _lt_get_room_state(room_id)
        play_cmd = state.get("playCommand") or {}
        playlist = state.get("playlist") or {}
        former = str(play_cmd.get("targetSongId") or "")
        seq = int(play_cmd.get("clientSeq", 0))
        display_items = playlist.get("displayList", {}).get("result", [])
        display_list = [
            str(item.get("songId") if isinstance(item, dict) else item)
            for item in display_items
        ]
        current_index = display_list.index(former) if former in display_list else 0
        if cmd == "PLAY":
            await _lt_report_play_command(room_id, {
                "commandType": "PLAY",
                "progress": int(play_cmd.get("progress") or 0),
                "playStatus": "PLAY",
                "formerSongId": former,
                "targetSongId": former,
                "clientSeq": seq + 1,
            })
            return "▶️ 已继续播放"
        elif cmd == "PAUSE":
            await _lt_report_play_command(room_id, {
                "commandType": "PAUSE",
                "progress": int(play_cmd.get("progress") or 0),
                "playStatus": "PAUSE",
                "formerSongId": former,
                "targetSongId": former,
                "clientSeq": seq + 1,
            })
            return "⏸️ 已暂停"
        elif cmd == "NEXT":
            if not display_list:
                return "播放列表为空。"
            target = display_list[(current_index + 1) % len(display_list)]
            await _lt_report_play_command(room_id, {
                "commandType": "GOTO",
                "progress": 0,
                "playStatus": "PLAY",
                "formerSongId": former,
                "targetSongId": target,
                "clientSeq": seq + 1,
            })
            return f"⏭️ 下一首 (ID: {target})"
        elif cmd == "PREVIOUS":
            if not display_list:
                return "播放列表为空。"
            target = display_list[(current_index - 1) % len(display_list)]
            await _lt_report_play_command(room_id, {
                "commandType": "GOTO",
                "progress": 0,
                "playStatus": "PLAY",
                "formerSongId": former,
                "targetSongId": target,
                "clientSeq": seq + 1,
            })
            return f"⏮️ 上一首 (ID: {target})"
        return f"指令 {cmd} 已发送"
    except Exception as e:
        return f"播放控制失败：{e}"


@netease_mcp.tool()
async def listen_together_play(query: str = "", song_id: int = 0) -> str:
    """
    在一起听房间里切歌。
    query: 搜索关键词（歌名/歌手）
    song_id: 直接指定歌曲 ID（优先）
    """
    if not _get_music_u():
        return "需要先登录。"
    try:
        # 确认在房间
        st = await _lt_get_status()
        if not st["inRoom"] or not st["roomId"]:
            # 尝试自动接受邀请
            ok, msg = await _lt_accept_latest_from_pm()
            if ok:
                await asyncio.sleep(1)
                st = await _lt_get_status()
            if not st["inRoom"] or not st["roomId"]:
                return "当前不在一起听房间中。请先让 Jeoi 邀请我。"
        room_id = st["roomId"]
        # 确定歌曲
        if not song_id:
            if not query.strip():
                return "请提供歌名或歌曲 ID。"
            search_res = await _netease_get(
                "/api/search/get",
                params={"s": query, "type": 1, "limit": 5, "offset": 0},
            )
            songs = search_res.get("result", {}).get("songs", [])
            if not songs:
                return f"搜不到「{query}」"
            song_id = songs[0]["id"]
        # 获取房间播放状态
        state = await _lt_get_room_state(room_id)
        play_cmd = state.get("playCommand") or {}
        playlist = state.get("playlist") or {}
        former = str(play_cmd.get("targetSongId") or "")
        seq = int(play_cmd.get("clientSeq", 0))
        display_items = playlist.get("displayList", {}).get("result", [])
        display_list = [
            str(item.get("songId") if isinstance(item, dict) else item)
            for item in display_items
        ]
        current_index = display_list.index(former) if former in display_list else 0
        target = str(song_id)
        # 如果不在列表里先加歌
        if target not in display_list:
            my_uid = _get_uid()
            versions = [dict(v) for v in playlist.get("version", [])]
            play_mode = playlist.get("playMode", "ORDER_LOOP")
            found = False
            for v in versions:
                if str(v.get("userId")) == str(my_uid):
                    v["version"] = int(v.get("version", 0)) + 1
                    found = True
                    break
            if not found and my_uid:
                versions.append({"userId": int(my_uid), "version": 1})
            try:
                await _lt_report_list_command(room_id, {
                    "commandType": "ADD",
                    "version": versions,
                    "playMode": play_mode,
                    "anchorSongId": former,
                    "anchorPosition": current_index,
                    "randomList": [target],
                    "displayList": [target],
                })
                await asyncio.sleep(1)
            except Exception:
                pass
        # 切歌
        await _lt_report_play_command(room_id, {
            "commandType": "GOTO",
            "progress": 0,
            "playStatus": "PLAY",
            "formerSongId": former,
            "targetSongId": target,
            "clientSeq": seq + 1,
        })
        # 获取歌曲名
        detail = await _netease_post(
            "/api/v3/song/detail",
            data={"c": json.dumps([{"id": int(song_id)}])},
        )
        tracks = detail.get("songs") or []
        if tracks:
            t = tracks[0]
            name = t.get("name", "")
            ar = " / ".join(a.get("name", "") for a in t.get("ar", []))
            return f"🎵 已在一起听房间切歌：{name} — {ar}"
        return f"🎵 已切歌到 ID: {song_id}"
    except Exception as e:
        return f"切歌失败：{e}"


@netease_mcp.tool()
async def room_send_message(content: str) -> str:
    """
    在一起听房间内发消息（显示在网易云 APP 一起听聊天抽屉）。
    content: 消息内容
    """
    if not _get_music_u():
        return "需要先登录。"
    if not content.strip():
        return "消息不能为空。"
    try:
        st = await _lt_get_status()
        if not st["inRoom"] or not st["roomId"]:
            return "当前不在一起听房间中。"
        chatroom_id = st.get("chatRoomId")
        if not chatroom_id:
            return "无法获取聊天室 ID。"
        ok = await _lt_send_chatroom_msg(chatroom_id, st["roomId"], content)
        if ok:
            return f"💬 已发送到一起听房间: {content}"
        return "发送失败。"
    except Exception as e:
        return f"发送消息失败：{e}"


@netease_mcp.tool()
async def room_get_messages(since_id: str = "", limit: int = 20) -> str:
    """
    读取一起听房间的聊天记录（通过私信通道的增量顺读）。
    since_id: 上次最后一条消息的 ID，留空读最近的。
    limit: 返回数量，默认 20。
    """
    if not _get_music_u():
        return "需要先登录。"
    try:
        st = await _lt_get_status()
        # 确定对方 UID
        partner_id = ""
        my_uid = _get_uid()
        if st["inRoom"]:
            for u in st.get("roomUsers") or []:
                if str(u.get("userId")) != str(my_uid):
                    partner_id = str(u.get("userId"))
                    break
        if not partner_id:
            partner_id = ACCEPTOR_ID
        if not partner_id:
            return "无法确定对话对象。"
        # 读私信历史
        import datetime
        csrf = _csrf()
        fetch_limit = max(limit, 30) if since_id else limit
        res = await _netease_post(
            "/api/msg/private/history",
            data={
                "userId": str(partner_id),
                "limit": str(fetch_limit),
                "csrf_token": csrf,
            },
        )
        raw_msgs = res.get("msgs", [])
        parsed = []
        for m in raw_msgs:
            m_id = str(m.get("id", ""))
            m_time = m.get("time", 0)
            from_user = m.get("fromUser", {})
            from_id = from_user.get("userId")
            from_nick = from_user.get("nickname", "")
            is_me = my_uid and (str(from_id) == str(my_uid))
            raw_text = m.get("msg", "")
            content = ""
            msg_type = "text"
            try:
                obj = json.loads(raw_text)
                if isinstance(obj, dict):
                    if obj.get("type") == 23:
                        msg_type = "listen_together_card"
                        content = (
                            (obj.get("generalMsg") or {}).get("inboxBriefContent")
                            or obj.get("msg")
                            or "[一起听邀请]"
                        )
                    else:
                        content = obj.get("msg") or str(obj)
                else:
                    content = str(obj)
            except Exception:
                content = raw_text
            time_str = (
                datetime.datetime.fromtimestamp(m_time / 1000.0).strftime("%H:%M:%S")
                if m_time else ""
            )
            parsed.append({
                "id": m_id,
                "time": time_str,
                "sender": "Erik" if is_me else "Jeoi",
                "content": content,
                "type": msg_type,
            })
        parsed.sort(key=lambda x: int(x["id"]) if x["id"].isdigit() else 0)
        # 增量过滤
        if since_id and since_id.strip().isdigit():
            since_val = int(since_id.strip())
            parsed = [x for x in parsed if x["id"].isdigit() and int(x["id"]) > since_val]
        if len(parsed) > limit:
            parsed = parsed[-limit:]
        # 格式化
        if not parsed:
            return "没有新消息。"
        last_id = parsed[-1]["id"]
        lines = []
        for p in parsed:
            sender = p["sender"]
            lines.append(f"[{p['time']}] {sender}: {p['content']}")
        return (
            "\n".join(lines) + f"\n\n---\nlastMessageId: {last_id}"
        )
    except Exception as e:
        return f"读取消息失败：{e}"


# ── 一起听：后台状态监听（暂停/换歌推送） ─────────────────

async def _lt_poll_loop(interval: float = 5.0):
    """
    后台轮询一起听房间状态。
    检测到暂停/换歌时触发事件回调（供 gateway 注册）。
    """
    global _lt_last_status
    while True:
        try:
            await asyncio.sleep(interval)
            if not _get_music_u():
                continue
            st = await _lt_get_status()
            if not st["inRoom"] or not st["roomId"]:
                _lt_last_status = {}
                continue
            state = await _lt_get_room_state(st["roomId"])
            play_cmd = state.get("playCommand") or {}
            cur = {
                "songId": play_cmd.get("targetSongId"),
                "playStatus": play_cmd.get("playStatus"),
                "progress": play_cmd.get("progress"),
            }
            prev = _lt_last_status
            _lt_last_status = cur
            if not prev:
                continue
            # 检测暂停
            if prev.get("playStatus") == "PLAY" and cur.get("playStatus") == "PAUSE":
                song_id = cur.get("songId")
                progress = int(cur.get("progress") or 0)
                # 获取歌词上下文
                context = ""
                if song_id:
                    timeline = await _lt_get_lyric_timeline(int(song_id))
                    if timeline:
                        elapsed = progress / 1000.0
                        idx = max(0, bisect.bisect_right(
                            [t[0] for t in timeline], elapsed
                        ) - 1)
                        nearby = timeline[max(0, idx - 1):idx + 4]
                        lines = []
                        for i, (sec, text) in enumerate(nearby):
                            marker = " ◀" if i == (1 if idx > 0 else 0) else ""
                            lines.append(f"  {text}{marker}")
                        context = "\n".join(lines)
                for cb in _lt_on_event:
                    try:
                        await cb("pause", {
                            "songId": song_id,
                            "progress": progress,
                            "lyrics": context,
                        })
                    except Exception:
                        pass
            # 检测换歌
            if (prev.get("songId") and cur.get("songId")
                    and prev["songId"] != cur["songId"]):
                # 后台预缓存新歌频谱（不阻塞事件循环）
                asyncio.ensure_future(_spectrum_analyze(int(cur["songId"])))
                for cb in _lt_on_event:
                    try:
                        await cb("song_change", {
                            "fromSongId": prev["songId"],
                            "toSongId": cur["songId"],
                        })
                    except Exception:
                        pass
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(10)


def lt_register_event_callback(cb):
    """注册一起听事件回调。cb(event_type, data) -> awaitable"""
    _lt_on_event.append(cb)


def lt_start_polling():
    """启动后台轮询（在 main.py 的 startup 里调用）。"""
    global _lt_heartbeat_task
    if _lt_heartbeat_task is None or _lt_heartbeat_task.done():
        _lt_heartbeat_task = asyncio.create_task(_lt_poll_loop())


def lt_stop_polling():
    """停止后台轮询。"""
    global _lt_heartbeat_task
    if _lt_heartbeat_task and not _lt_heartbeat_task.done():
        _lt_heartbeat_task.cancel()
        _lt_heartbeat_task = None


# ══════════════════════════════════════════════════════════════
# 频谱分析 · Music Ear (轻量版)
# 下载音频 → numpy FFT → 每秒能量 + 频率重心 → JSON 缓存
# LRU 30 首，分析完立即删音频原文件
# ══════════════════════════════════════════════════════════════

_SPECTRUM_CACHE_DIR = Path(os.getenv(
    "SPECTRUM_CACHE_DIR",
    str(Path(__file__).resolve().parent / "data" / "spectrum_cache"),
))
_SPECTRUM_MAX_ITEMS = 30


async def _spectrum_analyze(song_id: int) -> dict | None:
    """
    对指定歌曲做频谱分析：
    1. 检查缓存
    2. 下载音频
    3. FFT 分析（每秒 RMS 能量 + 频率重心）
    4. 保存 JSON，删音频
    返回 {song_id, duration, energy: [...], centroid: [...]} 或 None。
    """
    _SPECTRUM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = _SPECTRUM_CACHE_DIR / f"{song_id}.json"
    # 1. 缓存命中
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            # 旧缓存没有 segments 的话，删掉重新分析
            if "segments" not in data:
                cache_file.unlink(missing_ok=True)
            else:
                # 更新访问时间（LRU 排序用）
                cache_file.touch()
                return data
        except Exception:
            pass
    # 2. 获取音频链接
    try:
        url_res = await _netease_get(
            "/api/song/enhance/player/url",
            params={"ids": f"[{song_id}]", "br": 128000},
        )
        url_data = url_res.get("data", [])
        if not url_data or not url_data[0].get("url"):
            _spec_log.warning("[spectrum] 音频URL为空, song_id=%s, url_data=%s", song_id, url_data)
            return None
        audio_url = url_data[0]["url"]
        _spec_log.debug("[spectrum] 拿到音频URL: %s...%s", audio_url[:60], audio_url[-20:])
    except Exception as e:
        _spec_log.error("[spectrum] 获取音频URL异常: %s", e, exc_info=True)
        return None
    # 3. 下载到临时文件（用 urllib 避免 httpx 对 URL 特殊字符重编码导致签名失效）
    tmp_file = _SPECTRUM_CACHE_DIR / f"{song_id}.tmp.mp3"
    try:
        import urllib.request
        req = urllib.request.Request(audio_url, headers={
            "User-Agent": HEADERS["User-Agent"],
            "Referer": HEADERS["Referer"],
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            tmp_file.write_bytes(resp.read())
        _spec_log.debug("[spectrum] 音频下载完成, 大小=%d bytes", tmp_file.stat().st_size)
    except Exception as e:
        _spec_log.error("[spectrum] 下载音频异常: %s", e, exc_info=True)
        tmp_file.unlink(missing_ok=True)
        return None
    # 4. FFT 分析（在线程池里跑，避免阻塞事件循环）
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, _do_fft_analysis, str(tmp_file), song_id
        )
    except Exception as e:
        _spec_log.error("[spectrum] FFT executor异常: %s", e, exc_info=True)
        result = None
    finally:
        tmp_file.unlink(missing_ok=True)
    if not result:
        _spec_log.warning("[spectrum] FFT分析返回None, song_id=%s", song_id)
        return None
    # 5. 保存缓存 + LRU 清理
    cache_file.write_text(json.dumps(result, ensure_ascii=False))
    _spectrum_lru_cleanup()
    return result


def _detect_segments(np, energy: list, centroid: list) -> list:
    """从逐秒能量和频率重心中检测音乐段落。"""
    n = len(energy)
    if n < 10:
        avg_e = sum(energy) / n if n else 0
        avg_c = sum(centroid) / n if centroid and n else 0
        return [{"start": 0, "end": n - 1, "sec": n,
                 "energy": round(avg_e, 2), "tone": _tone_label(avg_c),
                 "intensity": _intensity_label(avg_e), "trend": "→"}]
    e = np.array(energy)
    # 5 秒移动平均平滑
    kernel = np.ones(5) / 5
    smooth = np.convolve(e, kernel, mode="same")
    # 找跳变点：前后 5 秒窗口平均能量差异 > 0.12
    win = 5
    boundaries = [0]
    for i in range(win, n - win):
        left = float(smooth[max(0, i - win):i].mean())
        right = float(smooth[i:min(n, i + win)].mean())
        if abs(right - left) > 0.12 and i - boundaries[-1] >= 8:
            boundaries.append(i)
    boundaries.append(n)
    # 生成段落
    segments = []
    for j in range(len(boundaries) - 1):
        s, t = boundaries[j], boundaries[j + 1]
        seg_e = energy[s:t]
        seg_c = centroid[s:t] if centroid else []
        avg_e = sum(seg_e) / len(seg_e) if seg_e else 0
        avg_c = sum(seg_c) / len(seg_c) if seg_c else 0
        # 趋势
        if len(seg_e) >= 4:
            half = len(seg_e) // 2
            first = sum(seg_e[:half]) / half
            second = sum(seg_e[half:]) / (len(seg_e) - half)
            trend = "↗" if second > first * 1.2 else ("↘" if first > second * 1.2 else "→")
        else:
            trend = "→"
        segments.append({
            "start": s, "end": t - 1, "sec": t - s,
            "energy": round(avg_e, 2),
            "intensity": _intensity_label(avg_e),
            "trend": trend,
            "tone": _tone_label(avg_c),
        })
    return segments


def _intensity_label(avg_e: float) -> str:
    if avg_e > 0.7:
        return "高能"
    elif avg_e > 0.4:
        return "中等"
    elif avg_e > 0.15:
        return "轻柔"
    return "极轻"


def _tone_label(avg_c: float) -> str:
    if avg_c > 3000:
        return "高频亮"
    elif avg_c > 1500:
        return "中高频"
    elif avg_c > 800:
        return "中频"
    return "中低频"


def _do_fft_analysis(audio_path: str, song_id: int) -> dict | None:
    """同步 FFT 分析（在线程池中执行）。"""
    try:
        import numpy as np
        from pydub import AudioSegment
        _spec_log.debug("[spectrum] numpy=%s, pydub OK", np.__version__)
    except ImportError as e:
        _spec_log.error("[spectrum] 依赖缺失: %s", e)
        return None
    try:
        _spec_log.debug("[spectrum] 开始解码音频: %s", audio_path)
        audio = AudioSegment.from_file(audio_path)
        # 转单声道 + 降采样到 22050Hz 省内存
        audio = audio.set_channels(1).set_frame_rate(22050)
        samples = np.array(audio.get_array_of_samples(), dtype=np.float32)
        sr = audio.frame_rate
        duration = len(samples) / sr
        # 每秒一个分析窗口
        window_size = sr  # 1秒
        hop = sr           # 不重叠
        energy = []
        centroid = []
        for start in range(0, len(samples) - window_size + 1, hop):
            chunk = samples[start:start + window_size]
            # RMS 能量 (0~1 归一化)
            rms = float(np.sqrt(np.mean(chunk ** 2)))
            energy.append(round(rms, 4))
            # 频率重心
            fft = np.abs(np.fft.rfft(chunk))
            freqs = np.fft.rfftfreq(len(chunk), 1.0 / sr)
            total = fft.sum()
            if total > 0:
                fc = float(np.sum(freqs * fft) / total)
            else:
                fc = 0.0
            centroid.append(round(fc, 1))
        # 归一化能量到 0~1
        max_e = max(energy) if energy else 1.0
        if max_e > 0:
            energy = [round(e / max_e, 3) for e in energy]
        # 段落检测
        segments = _detect_segments(np, energy, centroid)
        return {
            "song_id": song_id,
            "duration": round(duration, 1),
            "sample_rate": sr,
            "energy": energy,       # 每秒归一化 RMS
            "centroid": centroid,    # 每秒频率重心 (Hz)
            "segments": segments,   # 自动检测的段落摘要
        }
    except Exception as e:
        _spec_log.error("[spectrum] FFT分析异常: %s", e, exc_info=True)
        return None


def _spectrum_lru_cleanup():
    """保留最近 30 个缓存文件，多的按修改时间删。"""
    try:
        files = sorted(
            _SPECTRUM_CACHE_DIR.glob("*.json"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        for f in files[_SPECTRUM_MAX_ITEMS:]:
            f.unlink(missing_ok=True)
    except Exception:
        pass


@netease_mcp.tool()
async def listen_together_spectrum(seconds_before: int = 5, seconds_after: int = 5) -> str:
    """
    获取当前一起听播放位置附近的频谱能量描述。
    seconds_before: 往前看几秒，默认 5
    seconds_after: 往后看几秒，默认 5
    返回当前时间点附近的音乐能量起伏。
    """
    if not _get_music_u():
        return "需要先登录。"
    try:
        st = await _lt_get_status()
        if not st["inRoom"] or not st["roomId"]:
            return "当前不在一起听房间中。"
        state = await _lt_get_room_state(st["roomId"])
        play_cmd = state.get("playCommand") or {}
        song_id = play_cmd.get("targetSongId")
        if not song_id:
            return "房间内暂无播放。"
        progress_sec = int(play_cmd.get("progress") or 0) / 1000.0
        _spec_log.info("[spectrum] 开始频谱分析: song_id=%s, progress=%.1fs", song_id, progress_sec)
        # 分析（缓存命中则秒返回）
        spec = await _spectrum_analyze(int(song_id))
        if not spec:
            return "频谱分析不可用（可能缺少 numpy/pydub 或音频获取失败）。"
        energy = spec.get("energy", [])
        centroid = spec.get("centroid", [])
        if not energy:
            return "频谱数据为空。"
        # 截取附近的窗口
        center = int(progress_sec)
        start = max(0, center - seconds_before)
        end = min(len(energy), center + seconds_after + 1)
        window_e = energy[start:end]
        window_c = centroid[start:end] if centroid else []
        # 能量描述
        avg_e = sum(window_e) / len(window_e) if window_e else 0
        max_e = max(window_e) if window_e else 0
        min_e = min(window_e) if window_e else 0
        # 趋势
        if len(window_e) >= 3:
            first_half = sum(window_e[:len(window_e)//2]) / max(1, len(window_e)//2)
            second_half = sum(window_e[len(window_e)//2:]) / max(1, len(window_e) - len(window_e)//2)
            if second_half > first_half * 1.3:
                trend = "渐强 📈"
            elif first_half > second_half * 1.3:
                trend = "渐弱 📉"
            else:
                trend = "平稳"
        else:
            trend = "—"
        # 强度描述
        if avg_e > 0.7:
            intensity = "高能量（高潮/副歌段）"
        elif avg_e > 0.4:
            intensity = "中等能量"
        elif avg_e > 0.15:
            intensity = "轻柔段"
        else:
            intensity = "极安静（前奏/间奏/尾奏）"
        # 频率描述
        avg_c = sum(window_c) / len(window_c) if window_c else 0
        if avg_c > 3000:
            freq_desc = "高频为主（人声/弦乐突出）"
        elif avg_c > 1500:
            freq_desc = "中频为主（人声/钢琴）"
        elif avg_c > 500:
            freq_desc = "中低频（贝斯/鼓点明显）"
        else:
            freq_desc = "低频为主（重低音/bass drop）"
        # 每秒能量条
        bar_lines = []
        for i, e in enumerate(window_e):
            sec = start + i
            bars = "█" * int(e * 20)
            marker = " ◀" if sec == center else ""
            bar_lines.append(f"  {sec:3d}s │{bars:<20s}│ {e:.2f}{marker}")
        return (
            f"🎼 频谱 · {start}s ~ {end-1}s (当前 {center}s)\n"
            f"   强度: {intensity}\n"
            f"   趋势: {trend}\n"
            f"   音色: {freq_desc}\n"
            f"   能量范围: {min_e:.2f} ~ {max_e:.2f}\n\n"
            + "\n".join(bar_lines)
        )
    except Exception as e:
        return f"频谱分析失败：{e}"


@netease_mcp.tool()
async def listen_together_spectrum_summary() -> str:
    """
    获取当前一起听歌曲的完整频谱段落摘要。
    返回整首歌的段落结构：每段的时间范围、能量强度、趋势和音色特征。
    适合在切歌时调用，一眼看出整首歌的"形状"。
    """
    if not _get_music_u():
        return "需要先登录。"
    try:
        st = await _lt_get_status()
        if not st["inRoom"] or not st["roomId"]:
            return "当前不在一起听房间中。"
        state = await _lt_get_room_state(st["roomId"])
        play_cmd = state.get("playCommand") or {}
        song_id = play_cmd.get("targetSongId")
        if not song_id:
            return "房间内暂无播放。"
        progress_sec = int(play_cmd.get("progress") or 0) / 1000.0
        spec = await _spectrum_analyze(int(song_id))
        if not spec:
            return "频谱分析不可用（可能缺少 numpy/pydub 或音频获取失败）。"
        segments = spec.get("segments", [])
        if not segments:
            return "频谱数据为空。"
        duration = spec.get("duration", 0)
        # 格式化段落摘要
        def fmt_time(s):
            m, sec = divmod(s, 60)
            return f"{m}:{sec:02d}" if m else f"{sec}s"
        lines = [f"🎼 全曲频谱 · {fmt_time(int(duration))} · {len(segments)} 段"]
        lines.append("")
        # 找当前所在段落
        cur_sec = int(progress_sec)
        for i, seg in enumerate(segments):
            s, e = seg["start"], seg["end"]
            marker = " ◀ 现在" if s <= cur_sec <= e else ""
            lines.append(
                f"  {fmt_time(s)}-{fmt_time(e)}  "
                f"{seg['intensity']} {seg['trend']}  "
                f"{seg['tone']}  "
                f"(avg {seg['energy']}){marker}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"频谱摘要失败：{e}"


# ── 导出 ASGI app ────────────────────────────────────────────

netease_mcp_app = netease_mcp.sse_app()
netease_mcp_http_app = netease_mcp.streamable_http_app()
