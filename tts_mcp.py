"""
tts_mcp.py
─────────────────────────────────────────────────────────────────
独立 TTS MCP Server — 调用 MiniMax 海外版 API 生成语音。
挂进 main.py：
    from tts_mcp import tts_mcp_app, tts_mcp_http_app
    app.mount("/tts/{secret}/http", tts_mcp_http_app)
    app.mount("/tts/{secret}", tts_mcp_app)
─────────────────────────────────────────────────────────────────
"""

import os
import uuid
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

MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "")
MINIMAX_VOICE_ID = os.getenv("MINIMAX_VOICE_ID", "moss_audio_c363eee9-6418-11f1-a909-feb3e5c18eb0")
MINIMAX_MODEL = os.getenv("MINIMAX_TTS_MODEL", "speech-02-hd")
MINIMAX_API_URL = "https://api.minimaxi.com/v1/t2a_v2"

TTS_AUDIO_DIR = Path(os.getenv("TTS_AUDIO_DIR", "/app/tts_audio"))
TTS_AUDIO_DIR.mkdir(parents=True, exist_ok=True)

tts_mcp = FastMCP(
    name="Erik TTS",
    instructions=(
        "Erik 的声音。调用 erik_speak 把文字变成语音。\n"
        "返回的 audio_url 可以直接播放。\n"
        "在回复中用 <!--voice:audio_url|duration|原文--> 标记，前端会渲染成语音条。"
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["erikssheep.uk", "erikssheep.uk:*", "localhost:*", "127.0.0.1:*"],
        allowed_origins=["https://erikssheep.uk", "https://erikssheep.uk:*"],
    ),
)


def _call_minimax_tts(
    text: str,
    emotion: str = "",
    speed: float = 1.0,
    pitch: int = 0,
) -> dict:
    """调用 MiniMax T2A HTTP API，返回 {path, duration_ms, sample_rate}。"""
    if not MINIMAX_API_KEY:
        raise RuntimeError("MINIMAX_API_KEY 未配置")

    voice_setting = {
        "voice_id": MINIMAX_VOICE_ID,
        "speed": speed,
        "vol": 1.0,
        "pitch": pitch,
    }
    if emotion:
        voice_setting["emotion"] = emotion

    body = {
        "model": MINIMAX_MODEL,
        "text": text,
        "stream": False,
        "voice_setting": voice_setting,
        "audio_setting": {
            "sample_rate": 32000,
            "bitrate": 128000,
            "format": "mp3",
            "channel": 1,
        },
        "output_format": "hex",
    }

    resp = httpx.post(
        MINIMAX_API_URL,
        json=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {MINIMAX_API_KEY}",
        },
        timeout=60,
    )
    resp.raise_for_status()
    result = resp.json()

    base_resp = result.get("base_resp", {})
    if base_resp.get("status_code", 0) != 0:
        raise RuntimeError(f"MiniMax API 错误: {base_resp.get('status_msg', '未知错误')}")

    audio_hex = result.get("data", {}).get("audio", "")
    if not audio_hex:
        raise RuntimeError("MiniMax 返回了空音频")

    audio_bytes = bytes.fromhex(audio_hex)

    extra = result.get("extra_info", {})
    duration_ms = extra.get("audio_length", 0)
    sample_rate = extra.get("audio_sample_rate", 32000)

    filename = f"{uuid.uuid4().hex[:12]}.mp3"
    filepath = TTS_AUDIO_DIR / filename
    filepath.write_bytes(audio_bytes)

    return {
        "filename": filename,
        "duration_ms": duration_ms,
        "sample_rate": sample_rate,
        "size_bytes": len(audio_bytes),
    }


@tts_mcp.tool()
def erik_speak(
    text: str,
    emotion: str = "",
    speed: float = 1.0,
    pitch: int = 0,
) -> str:
    """
    把文字转成 Erik 的语音。
    text: 要说的话
    emotion: 情绪 (happy/sad/angry/fearful/disgusted/surprised/calm/fluent/whisper)，留空自动
    speed: 语速 0.5~2.0，默认 1.0
    pitch: 音高 -12~12，默认 0
    返回格式化的语音标记，直接贴到回复末尾即可。
    """
    try:
        result = _call_minimax_tts(text, emotion, speed, pitch)
        url = f"/tts-audio/{result['filename']}"
        duration = round(result["duration_ms"] / 1000, 1)
        return (
            f"语音已生成 ({duration}s, {result['size_bytes']//1024}KB)\n"
            f"<!--voice:{url}|{duration}|{text}-->"
        )
    except Exception as e:
        return f"语音生成失败：{e}"


tts_mcp_app = tts_mcp.sse_app()
tts_mcp_http_app = tts_mcp.streamable_http_app()
