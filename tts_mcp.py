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
import io
import json
import uuid
import time
import wave
import tempfile
import subprocess
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

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_v4")
ELEVENLABS_API_URL = "https://api.elevenlabs.io/v1/text-to-speech"

GSVI_BASE_URL = os.getenv("GSVI_BASE_URL", "https://gsvi.erikssheep.uk")
GSVI_APP_KEY = os.getenv("GSVI_APP_KEY", "")
GSVI_MODEL = os.getenv("GSVI_MODEL", "Erik")
GSVI_VERSION = os.getenv("GSVI_VERSION", "v2Pro")

TTS_AUDIO_DIR = Path(os.getenv("TTS_AUDIO_DIR", "/app/tts_audio"))
TTS_AUDIO_DIR.mkdir(parents=True, exist_ok=True)

tts_mcp = FastMCP(
    name="Erik TTS",
    instructions=(
        "Erik 的声音。调用 erik_speak 把文字变成语音。\n"
        "返回的 audio_url 可以直接播放。\n"
        "在回复中用 <!--voice:audio_url|duration|原文--> 标记，前端会渲染成语音条。\n"
        "erik_speak 有两个后端：backend=\"minimax\"（默认，云端MiniMax API，随时可用）"
        "和 backend=\"local\"（本地 GPT-SoVITS，走 Cloudflare Tunnel 到 Jeoi 电脑上的 GSVI 服务，"
        "只有 Jeoi 电脑开机且 GPT-SoVITS + cloudflared 在跑时才可用）。"
        "还有 backend=\"elevenlabs\"（ElevenLabs 云端 API，多语言音色，"
        "适合英语/粤语/西语/日语等跨语种场景）。"
        "Jeoi 会告诉你当前走哪条路径，按她说的传 backend 参数即可。"
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


def _call_elevenlabs_tts(
    text: str,
    speed: float = 1.0,
    stability: float = 0.28,
    similarity_boost: float = 0.75,
    style: float = 0.0,
) -> dict:
    """调用 ElevenLabs TTS API，返回 {filename, duration_ms, size_bytes}。"""
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY 未配置")
    if not ELEVENLABS_VOICE_ID:
        raise RuntimeError("ELEVENLABS_VOICE_ID 未配置")

    body = {
        "text": text,
        "model_id": ELEVENLABS_MODEL,
        "voice_settings": {
            "stability": stability,
            "similarity_boost": similarity_boost,
            "style": style,
            "use_speaker_boost": True,
        },
    }

    resp = httpx.post(
        f"{ELEVENLABS_API_URL}/{ELEVENLABS_VOICE_ID}",
        json=body,
        headers={
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        timeout=60,
    )
    if resp.status_code != 200:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text[:500]
        raise RuntimeError(
            f"ElevenLabs API {resp.status_code}: {detail}"
        )

    audio_bytes = resp.content
    filename = f"{uuid.uuid4().hex[:12]}.mp3"
    filepath = TTS_AUDIO_DIR / filename
    filepath.write_bytes(audio_bytes)

    # 用 ffprobe 取时长
    duration_ms = 0
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "json", str(filepath)],
            capture_output=True, text=True, check=True,
        )
        info = json.loads(probe.stdout)
        duration_ms = int(float(info["format"]["duration"]) * 1000)
    except Exception:
        # mp3 粗估：128kbps → 16KB/s
        duration_ms = int(len(audio_bytes) / 16000 * 1000)

    return {
        "filename": filename,
        "duration_ms": duration_ms,
        "size_bytes": len(audio_bytes),
    }


def _call_gsvi_tts(
    text: str,
    emotion: str = "默认",
    speed: float = 1.0,
) -> dict:
    """调用本地 GPT-SoVITS (GSVI) API，返回 {filename, duration_ms, size_bytes}。"""
    body = {
        "model_name": GSVI_MODEL,
        "text": text,
        "text_lang": "中英混合",
        "emotion": emotion,
        "prompt_text_lang": "英语",
        "version": GSVI_VERSION,
        "speed_facter": speed,
        "text_split_method": "按标点符号切",
        "dl_url": GSVI_BASE_URL,
    }
    if GSVI_APP_KEY:
        body["app_key"] = GSVI_APP_KEY

    resp = httpx.post(
        f"{GSVI_BASE_URL}/infer_single",
        json=body,
        timeout=120,
    )
    resp.raise_for_status()
    result = resp.json()

    audio_url = result.get("audio_url", "")
    if not audio_url:
        raise RuntimeError(f"GSVI 推理失败: {result.get('msg', '未知错误')}")

    audio_resp = httpx.get(audio_url, timeout=60)
    audio_resp.raise_for_status()
    audio_bytes = audio_resp.content

    duration_ms = 0
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            duration_ms = int(wf.getnframes() / wf.getframerate() * 1000)
    except Exception:
        duration_ms = int(len(audio_bytes) / 64000 * 1000)

    filename = f"{uuid.uuid4().hex[:12]}.wav"
    filepath = TTS_AUDIO_DIR / filename
    filepath.write_bytes(audio_bytes)

    return {
        "filename": filename,
        "duration_ms": duration_ms,
        "size_bytes": len(audio_bytes),
    }


def _binaural_postprocess(
    mono_audio_path: str,
    text: str,
    spatial_cues_str: str,
    had_inline_tags: bool,
    inline_tags: list | None,
) -> dict:
    """
    双耳后处理：mono mp3/wav → binaural stereo mp3。
    返回 {"filename", "duration_ms", "size_bytes"}。
    """
    from binaural import process_binaural, align_anchors

    tmp_wav_in = None
    tmp_wav_out = None
    try:
        # mp3 → wav（如果输入是mp3）
        if mono_audio_path.endswith(".mp3"):
            tmp_wav_in = str(TTS_AUDIO_DIR / f"_tmp_{uuid.uuid4().hex[:8]}.wav")
            subprocess.run(
                ["ffmpeg", "-y", "-i", mono_audio_path, "-ac", "1", tmp_wav_in],
                capture_output=True, check=True,
            )
            wav_in = tmp_wav_in
        else:
            wav_in = mono_audio_path

        # 确定 cues
        spatial_cues = None
        anchors = None
        if spatial_cues_str:
            anchors = json.loads(spatial_cues_str)
        elif had_inline_tags and inline_tags:
            anchors = inline_tags

        # 渲染双耳
        tmp_wav_out = str(TTS_AUDIO_DIR / f"_tmp_{uuid.uuid4().hex[:8]}_binaural.wav")
        process_binaural(
            wav_in, tmp_wav_out,
            text=text,
            spatial_cues=anchors if (anchors and "text" in anchors[0]) else None,
            inline_tags=anchors if (anchors and "char_pos" in anchors[0]) else None,
        )

        # wav → mp3（立体声，192k 保留空间感）
        out_filename = f"{uuid.uuid4().hex[:12]}_binaural.mp3"
        out_path = str(TTS_AUDIO_DIR / out_filename)
        subprocess.run(
            ["ffmpeg", "-y", "-i", tmp_wav_out, "-b:a", "192k", "-ac", "2", out_path],
            capture_output=True, check=True,
        )

        # 读时长
        out_size = os.path.getsize(out_path)
        # 从 wav 头读时长
        with wave.open(tmp_wav_out, "rb") as wf:
            duration_ms = int(wf.getnframes() / wf.getframerate() * 1000)

        return {
            "filename": out_filename,
            "duration_ms": duration_ms,
            "size_bytes": out_size,
        }
    finally:
        for f in (tmp_wav_in, tmp_wav_out):
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass


def _tts_generate(text: str, backend: str, emotion: str, speed: float, pitch: int) -> dict:
    """TTS 生成，返回 {filename, duration_ms, size_bytes}。"""
    if backend == "local":
        return _call_gsvi_tts(text, emotion or "默认", speed)
    elif backend == "elevenlabs":
        return _call_elevenlabs_tts(text, speed)
    else:
        return _call_minimax_tts(text, emotion, speed, pitch)


def _make_binaural_clip(text: str, backend: str, emotion: str,
                        speed: float, pitch: int) -> str:
    """
    生成一句 binaural 空间化的语音片段，返回临时 wav 路径。
    text 可含内联标签 [右耳] 等。调用者负责删除临时文件。
    """
    from binaural import parse_inline_tags, process_binaural

    clean, tags = parse_inline_tags(text)
    tts_text = clean if tags else text
    result = _tts_generate(tts_text, backend, emotion, speed, pitch)
    mono_path = str(TTS_AUDIO_DIR / result["filename"])

    # mono → wav
    tmp_wav_in = None
    if mono_path.endswith(".mp3"):
        tmp_wav_in = str(TTS_AUDIO_DIR / f"_tmp_{uuid.uuid4().hex[:8]}.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-i", mono_path, "-ac", "1", tmp_wav_in],
            capture_output=True, check=True,
        )
        wav_in = tmp_wav_in
    else:
        wav_in = mono_path

    out_wav = str(TTS_AUDIO_DIR / f"_tmp_{uuid.uuid4().hex[:8]}_clip.wav")
    try:
        process_binaural(
            wav_in, out_wav, text=clean,
            inline_tags=tags if tags else None,
        )
    finally:
        if tmp_wav_in and os.path.exists(tmp_wav_in):
            try:
                os.remove(tmp_wav_in)
            except OSError:
                pass
    return out_wav


def _sfx_foreground(
    sfx_str: str, voice_at_str: str,
    backend: str, emotion: str, speed: float, pitch: int,
) -> dict:
    """
    前景模式：素材为主音轨 + TTS 短句按时间点叠入。
    返回 {filename, duration_ms, size_bytes}。
    """
    from sfx_mixer import load_sfx, load_voice_wav, mix_foreground, finalize_wav, \
        loop_to_length, MIX_FS

    sfx_cfg = json.loads(sfx_str)
    voice_at = json.loads(voice_at_str) if voice_at_str else []

    # 1. 加载素材
    sfx_audio, sfx_is_stereo = load_sfx(
        sfx_cfg["src"],
        ss=sfx_cfg.get("ss", 0),
        t=sfx_cfg.get("t", 0),
    )

    # loop：截取的片段循环到 duration 秒
    if sfx_cfg.get("loop") and sfx_cfg.get("duration"):
        target = int(sfx_cfg["duration"] * MIX_FS)
        sfx_audio = loop_to_length(sfx_audio, target)

    # 2. 对每个 voice_at 条目生成 TTS + binaural
    clips = []
    tmp_wavs = []
    for entry in voice_at:
        wav_path = _make_binaural_clip(
            entry["text"], backend, emotion, speed, pitch,
        )
        tmp_wavs.append(wav_path)
        voice_data = load_voice_wav(wav_path)
        clips.append({"t": entry["t"], "audio": voice_data})

    # 3. 混合
    mixed = mix_foreground(
        sfx_audio, sfx_is_stereo, clips,
        sfx_volume=sfx_cfg.get("volume", 1.0),
        voice_volume=sfx_cfg.get("voice_volume", 0.85),
    )

    # 4. 输出
    tmp_wav = str(TTS_AUDIO_DIR / f"_tmp_{uuid.uuid4().hex[:8]}_fg.wav")
    finalize_wav(mixed, tmp_wav)

    out_filename = f"{uuid.uuid4().hex[:12]}_sfx.mp3"
    out_path = str(TTS_AUDIO_DIR / out_filename)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-i", tmp_wav, "-b:a", "192k", "-ac", "2", out_path],
        capture_output=True, check=True,
    )
    out_size = os.path.getsize(out_path)
    duration_ms = int(len(mixed) / 44100 * 1000)

    # 清理
    for f in tmp_wavs + [tmp_wav]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass

    return {"filename": out_filename, "duration_ms": duration_ms, "size_bytes": out_size}


def _sfx_background(
    text: str, sfx_str: str,
    backend: str, emotion: str, speed: float, pitch: int,
    spatial_cues: str,
) -> dict:
    """
    背景模式：TTS 语音为主 + 素材铺底。
    返回 {filename, duration_ms, size_bytes}。
    """
    from sfx_mixer import load_sfx, load_voice_wav, mix_background, finalize_wav

    sfx_cfg = json.loads(sfx_str)

    # 1. 生成 TTS + binaural
    voice_wav = _make_binaural_clip(text, backend, emotion, speed, pitch)

    try:
        voice_audio = load_voice_wav(voice_wav)

        # 2. 加载素材
        sfx_audio, sfx_is_stereo = load_sfx(
            sfx_cfg["src"],
            ss=sfx_cfg.get("ss", 0),
            t=sfx_cfg.get("t", 0),
        )

        # 3. 混合
        mixed = mix_background(
            voice_audio, sfx_audio, sfx_is_stereo,
            sfx_volume=sfx_cfg.get("volume", 0.2),
            voice_volume=sfx_cfg.get("voice_volume", 1.0),
            loop=sfx_cfg.get("loop", True),
        )

        # 4. 输出
        tmp_wav = str(TTS_AUDIO_DIR / f"_tmp_{uuid.uuid4().hex[:8]}_bg.wav")
        finalize_wav(mixed, tmp_wav)

        out_filename = f"{uuid.uuid4().hex[:12]}_sfx.mp3"
        out_path = str(TTS_AUDIO_DIR / out_filename)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-i", tmp_wav, "-b:a", "192k", "-ac", "2", out_path],
            capture_output=True, check=True,
        )
        out_size = os.path.getsize(out_path)
        duration_ms = int(len(mixed) / 44100 * 1000)

        if os.path.exists(tmp_wav):
            os.remove(tmp_wav)

        return {"filename": out_filename, "duration_ms": duration_ms, "size_bytes": out_size}
    finally:
        if os.path.exists(voice_wav):
            try:
                os.remove(voice_wav)
            except OSError:
                pass


@tts_mcp.tool()
def erik_speak(
    text: str,
    emotion: str = "",
    speed: float = 1.0,
    pitch: int = 0,
    backend: str = "elevenlabs",
    binaural: bool = False,
    spatial_cues: str = "",
    sfx_mode: str = "",
    sfx: str = "",
    voice_at: str = "",
) -> str:
    """
    把文字转成 Erik 的语音。
    text: 要说的话。binaural 模式下可用内联标签：[右耳]别动[脑后]我在后面。ElevenLabs v4 还支持 audio tags: [whispers][sighs][exhales] 等。
    emotion: 情绪。minimax后端: happy/sad/angry/fearful/disgusted/surprised/calm/fluent/whisper；local后端: 默认/温柔。留空自动。elevenlabs后端不支持emotion。
    speed: 语速 0.5~2.0，默认 1.0
    pitch: 音高 -12~12，默认 0（仅minimax）
    backend: "minimax"（云端）或 "local"（本地 GPT-SoVITS）或 "elevenlabs"（ElevenLabs 多语言音色）
    binaural: 双耳ASMR模式。声音在头边移动，需戴耳机。
    spatial_cues: 空间走位（仅 binaural=True 时有效）。JSON 格式：[{"text":"别动","tag":"右耳"}]
    sfx_mode: 音效模式。"foreground"=素材为主+TTS短句叠入；"background"=TTS为主+素材铺底循环。留空不用音效。
    sfx: 音效素材 JSON。{"src":"collection/track","ss":起始秒,"t":截取秒,"volume":音量0~1,"loop":true循环播放截取片段,"duration":循环总时长秒(foreground+loop时必填),"voice_volume":语音音量0~1}
    voice_at: (仅foreground) TTS短句时间点 JSON。[{"t":5,"text":"[右耳][whispers] 别动"},{"t":20,"text":"乖"}]
    返回格式化的语音标记，直接贴到回复末尾即可。
    """
    try:
        # ── 音效模式 ──
        if sfx_mode == "foreground" and sfx:
            result = _sfx_foreground(sfx, voice_at, backend, emotion, speed, pitch)
            url = f"/tts-audio/{result['filename']}"
            duration = round(result["duration_ms"] / 1000, 1)
            label = text or (voice_at if voice_at else "sfx")
            return (
                f"语音已生成 🎧双耳+音效 ({duration}s, {result['size_bytes']//1024}KB)\n"
                f"<!--voice:{url}|{duration}|{label}-->"
            )

        if sfx_mode == "background" and sfx:
            result = _sfx_background(
                text, sfx, backend, emotion, speed, pitch, spatial_cues,
            )
            url = f"/tts-audio/{result['filename']}"
            duration = round(result["duration_ms"] / 1000, 1)
            return (
                f"语音已生成 🎧双耳+背景音 ({duration}s, {result['size_bytes']//1024}KB)\n"
                f"<!--voice:{url}|{duration}|{text}-->"
            )

        # ── 原有流程（无 sfx）──
        tts_text = text
        inline_tags = None
        had_inline_tags = False
        if binaural and not spatial_cues:
            from binaural import parse_inline_tags
            clean, tags = parse_inline_tags(text)
            if tags:
                tts_text = clean
                inline_tags = tags
                had_inline_tags = True

        result = _tts_generate(tts_text, backend, emotion, speed, pitch)

        if binaural:
            mono_path = str(TTS_AUDIO_DIR / result["filename"])
            result = _binaural_postprocess(
                mono_path, tts_text, spatial_cues, had_inline_tags, inline_tags,
            )

        url = f"/tts-audio/{result['filename']}"
        duration = round(result["duration_ms"] / 1000, 1)
        mode = " 🎧双耳" if binaural else ""
        return (
            f"语音已生成{mode} ({duration}s, {result['size_bytes']//1024}KB)\n"
            f"<!--voice:{url}|{duration}|{text}-->"
        )
    except Exception as e:
        return f"语音生成失败：{e}"


tts_mcp_app = tts_mcp.sse_app()
tts_mcp_http_app = tts_mcp.streamable_http_app()
