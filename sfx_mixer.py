"""
sfx_mixer.py — 音效混合引擎
────────────────────────────
两种模式：
- foreground: 素材为主音轨，TTS 短句按时间点叠入（ducking）
- background: TTS 为主音轨，素材铺底循环
"""

import json
import os
import subprocess
import tempfile
import numpy as np
from pathlib import Path
from scipy.io import wavfile
from scipy.signal import resample_poly
from math import gcd

SFX_DIR = Path(os.getenv("SFX_DIR", "/app/sfx"))
SFX_INDEX_PATH = SFX_DIR / "index.json"
MIX_FS = 44100  # 统一采样率

_index_cache = None


def _load_index() -> dict:
    global _index_cache
    if _index_cache is None:
        if SFX_INDEX_PATH.exists():
            with open(SFX_INDEX_PATH) as f:
                _index_cache = json.load(f)
        else:
            _index_cache = {"collections": {}, "clips": {}}
    return _index_cache


def invalidate_index():
    """素材有更新时手动刷新缓存。"""
    global _index_cache
    _index_cache = None


def _resolve_path(src: str) -> Path:
    """
    先查 index.json 里的 file 映射，再按名字搜索。
    "collection/track" → /app/sfx/collections/collection/track.mp3
    "clip_name"        → index.json clips[clip_name].file，或 /app/sfx/clips/clip_name.*
    """
    idx = _load_index()

    # 优先用 index.json 里的 file 字段（支持文件名带空格等情况）
    if "/" not in src and src in idx.get("clips", {}):
        mapped = idx["clips"][src].get("file")
        if mapped:
            p = SFX_DIR / mapped
            if p.exists():
                return p

    if "/" in src:
        collection, track = src.split("/", 1)
        base = SFX_DIR / "collections" / collection
    else:
        base = SFX_DIR / "clips"
        track = src

    for ext in (".mp3", ".wav", ".flac", ".m4a", ".ogg", ""):
        path = base / f"{track}{ext}"
        if path.exists():
            return path

    raise FileNotFoundError(f"素材不存在: {src}  (searched {base}/{track}.*)")


# ━━━━━━━━━━━━━━━━━━━ 加载 ━━━━━━━━━━━━━━━━━━━

def load_sfx(src: str, ss: float = 0, t: float = 0) -> tuple[np.ndarray, bool]:
    """
    加载音效素材，重采样到 MIX_FS。
    返回 (audio_float64, is_stereo)。
    """
    path = _resolve_path(src)
    tmp = tempfile.mktemp(suffix=".wav")
    try:
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        if ss > 0:
            cmd.extend(["-ss", str(ss)])
        cmd.extend(["-i", str(path)])
        if t > 0:
            cmd.extend(["-t", str(t)])
        cmd.extend(["-ar", str(MIX_FS), tmp])
        subprocess.run(cmd, capture_output=True, check=True)

        fs, data = wavfile.read(tmp)
        if np.issubdtype(data.dtype, np.integer):
            data = data.astype(np.float64) / np.iinfo(data.dtype).max
        else:
            data = data.astype(np.float64)

        is_stereo = data.ndim > 1 and data.shape[1] >= 2
        if is_stereo and data.shape[1] > 2:
            data = data[:, :2]
        return data, is_stereo
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def load_voice_wav(wav_path: str) -> np.ndarray:
    """
    加载已 binaural 空间化的 wav/mp3，重采样到 MIX_FS。
    返回 stereo float64 (samples, 2)。
    """
    # 先统一用 ffmpeg 转成 MIX_FS wav（处理 mp3 / 各种采样率）
    tmp = tempfile.mktemp(suffix=".wav")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-i", wav_path, "-ar", str(MIX_FS), tmp],
            capture_output=True, check=True,
        )
        fs, data = wavfile.read(tmp)
        if np.issubdtype(data.dtype, np.integer):
            data = data.astype(np.float64) / np.iinfo(data.dtype).max
        else:
            data = data.astype(np.float64)

        if data.ndim == 1:
            data = np.column_stack([data, data])
        return data
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ━━━━━━━━━━━━━━━━━━━ 工具 ━━━━━━━━━━━━━━━━━━━

def _to_stereo(audio: np.ndarray) -> np.ndarray:
    if audio.ndim == 1:
        return np.column_stack([audio, audio])
    return audio


def loop_to_length(audio: np.ndarray, target: int) -> np.ndarray:
    """循环到 target samples，末尾 50ms 淡出。"""
    if len(audio) >= target:
        out = audio[:target].copy()
    else:
        reps = (target // len(audio)) + 1
        out = (np.tile(audio, (reps, 1)) if audio.ndim > 1
               else np.tile(audio, reps))[:target].copy()

    fade = min(int(MIX_FS * 0.05), target)
    ramp = np.linspace(1, 0, fade)
    if out.ndim > 1:
        out[-fade:] *= ramp[:, None]
    else:
        out[-fade:] *= ramp
    return out


# ━━━━━━━━━━━━━━━━━━━ 混合 ━━━━━━━━━━━━━━━━━━━

def mix_foreground(
    sfx_audio: np.ndarray,
    sfx_is_stereo: bool,
    voice_clips: list[dict],
    sfx_volume: float = 1.0,
    voice_volume: float = 0.85,
) -> np.ndarray:
    """
    前景模式：素材为主，语音按时间点叠入（带 ducking）。
    voice_clips: [{"t": float, "audio": ndarray(stereo, MIX_FS)}]
    返回 (samples, 2)。
    """
    out = _to_stereo(sfx_audio).copy() * sfx_volume

    for clip in sorted(voice_clips, key=lambda c: c["t"]):
        voice = clip["audio"]
        start = int(clip["t"] * MIX_FS)
        end = min(start + len(voice), len(out))
        if start >= len(out) or end <= start:
            continue
        vlen = end - start

        # ducking：语音前后 0.3s 渐变，中间素材压到 50%
        fade_n = int(0.3 * MIX_FS)
        ds = max(0, start - fade_n)
        de = min(len(out), end + fade_n)
        region = de - ds
        duck = np.ones(region)
        pre = start - ds
        if pre > 0:
            duck[:pre] = np.linspace(1, 0.5, pre)
        duck[pre:pre + vlen] = 0.5
        post = de - end
        if post > 0:
            duck[pre + vlen:] = np.linspace(0.5, 1, post)
        out[ds:de] *= duck[:, None]

        out[start:end] += voice[:vlen] * voice_volume

    return out


def mix_background(
    voice_audio: np.ndarray,
    sfx_audio: np.ndarray,
    sfx_is_stereo: bool,
    sfx_volume: float = 0.2,
    voice_volume: float = 1.0,
    loop: bool = True,
) -> np.ndarray:
    """
    背景模式：语音为主，素材铺底。
    返回 (samples, 2)。
    """
    voice = _to_stereo(voice_audio) * voice_volume
    total = len(voice)
    sfx = _to_stereo(sfx_audio)

    if loop:
        sfx = loop_to_length(sfx, total)
    elif len(sfx) < total:
        sfx = np.pad(sfx, ((0, total - len(sfx)), (0, 0)))
    else:
        sfx = sfx[:total]

    return voice + sfx * sfx_volume


# ━━━━━━━━━━━━━━━━━━━ 输出 ━━━━━━━━━━━━━━━━━━━

def finalize_wav(audio: np.ndarray, output_path: str):
    """限幅 -1 dBFS → int16 wav。"""
    peak = np.abs(audio).max()
    if peak > 0:
        audio = audio * min(1.0, 0.89 / peak)
    wavfile.write(output_path, MIX_FS,
                  np.int16(np.clip(audio * 32767, -32768, 32767)))
