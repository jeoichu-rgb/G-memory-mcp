"""
binaural.py — 双耳立体声渲染 + Whisper 文本对齐
────────────────────────────────────────────────
把单声道语音渲染成 ASMR 双耳音频，声音可以在头边移动。

核心渲染逻辑基于 https://github.com/Saekisui/binaural-voice (MIT License)
HRIR 数据来源：Neumann KU100 近场球面测量
  J. M. Arend et al., Zenodo 2020, doi:10.5281/zenodo.4297951 (CC BY 4.0)
"""

import json
import os
import re
import random
import subprocess
import numpy as np
from pathlib import Path
from scipy.io import wavfile
from scipy.signal import fftconvolve, resample_poly

# ── 位置定义 ──
# 方位角（逆时针，0=前 90=左 180=后 270=右）+ 默认距离 m
PLACES = {
    "左耳": (90, 0.25), "右耳": (270, 0.25),
    "脑后": (180, 0.31), "面前": (0, 0.42),
}
NEAR, FAR = 0.25, 0.5              # 贴近 / 退开 到的距离
HALF_TURN = 4.5                     # 绕半圈用多少"出声秒"
DIST_MOVE = 1.5                     # 只改远近用多少出声秒
DRIFT_DEG, DRIFT_M = 5, 0.015      # 停住时的微晃幅度
# 数据集各距离的增益归一化（让远近有自然音量差）
GAINS = [1.00, 0.33, 0.25, 0.16, 0.095]

HRIR_DIR = Path(os.getenv("HRIR_DIR", Path(__file__).parent / "hrir"))
TAG_RE = re.compile(r"\[(左耳|右耳|脑后|面前|贴近|退开)\]")

# ── 懒加载缓存 ──
_hrir_cache: dict = {}
_whisper_model = None


# ━━━━━━━━━━━━━━━━━━━━━ HRIR 加载 ━━━━━━━━━━━━━━━━━━━━━

def _load_hrir(target_fs: int):
    if target_fs in _hrir_cache:
        return _hrir_cache[target_fs]
    npz = HRIR_DIR / "ku100_nearfield_circ360.npz"
    if not npz.exists():
        raise FileNotFoundError(
            f"HRIR 数据不存在：{npz}\n"
            "下载：wget https://github.com/Saekisui/binaural-voice/raw/main/"
            "hrir/ku100_nearfield_circ360.npz -O /app/hrir/ku100_nearfield_circ360.npz"
        )
    data = np.load(str(npz))
    dists = data["dists"]
    ir = data["ir"].astype(np.float64)
    orig_fs = int(data["fs"])
    gain = np.array(GAINS)[:, None, None, None]
    H = resample_poly(ir, target_fs, orig_fs, axis=3) * gain if target_fs != orig_fs else ir * gain
    _hrir_cache[target_fs] = (dists, H)
    return dists, H


# ━━━━━━━━━━━━━━━━━━━━━ 标签解析 ━━━━━━━━━━━━━━━━━━━━━

def parse_inline_tags(text: str):
    """
    "[右耳]别动[脑后]我在后面" → ("别动我在后面", [{char_pos:0, tag:"右耳"}, ...])
    """
    tags = []
    parts = []
    last = 0
    offset = 0
    for m in TAG_RE.finditer(text):
        before = text[last:m.start()]
        parts.append(before)
        offset += len(before)
        tags.append({"char_pos": offset, "tag": m.group(1)})
        last = m.end()
    parts.append(text[last:])
    return "".join(parts), tags


# ━━━━━━━━━━━━━━━━━━━━━ Whisper 对齐 ━━━━━━━━━━━━━━━━━━━━━

def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    return _whisper_model


def align_anchors(wav_path: str, anchors: list[dict]) -> list[dict]:
    """
    用 faster-whisper 做词级对齐，把文本锚点转为时间戳。

    anchors 格式二选一：
      文本锚点: [{"text": "哈哈", "tag": "脑后"}, ...]
      字符位置: [{"char_pos": 4, "tag": "脑后"}, ...]   (来自 parse_inline_tags)

    返回: [{"t": 2.31, "tag": "脑后"}, ...]
    """
    if not anchors:
        return []

    model = _get_whisper()
    segments, _ = model.transcribe(wav_path, word_timestamps=True, language="zh")

    words = []
    for seg in segments:
        if seg.words:
            for w in seg.words:
                words.append({"text": w.word.strip(), "start": w.start})
    if not words:
        return []

    joined = "".join(w["text"] for w in words)
    cues = []

    if "text" in anchors[0]:
        # ── 文本锚点：在识别结果里找目标文字 ──
        for anchor in anchors:
            pos = joined.find(anchor["text"])
            if pos < 0:
                continue
            acc = 0
            for w in words:
                if acc + len(w["text"]) > pos:
                    cues.append({"t": w["start"], "tag": anchor["tag"]})
                    break
                acc += len(w["text"])

    elif "char_pos" in anchors[0]:
        # ── 字符位置：按位置映射到时间 ──
        # 建立逐字时间表
        char_times = []
        for w in words:
            n = max(len(w["text"]), 1)
            for i in range(n):
                char_times.append(w["start"])
        for anchor in anchors:
            idx = min(anchor["char_pos"], len(char_times) - 1)
            if 0 <= idx < len(char_times):
                cues.append({"t": char_times[idx], "tag": anchor["tag"]})

    return cues


# ━━━━━━━━━━━━━━━━━━━━━ 双耳渲染 ━━━━━━━━━━━━━━━━━━━━━

def render_binaural(mono_wav: str, out_wav: str, cues: list[dict] | None = None):
    """
    单声道 WAV → 双耳立体声 WAV。

    cues: [{"t": 秒, "tag": "右耳"}, ...] 或 None（随机走位）
    """
    fs, x = wavfile.read(mono_wav)
    if np.issubdtype(x.dtype, np.integer):
        x = x / np.iinfo(x.dtype).max
    x = x.astype(np.float64)
    if x.ndim > 1:
        x = x.mean(axis=1)

    # ── 出声检测（10ms 帧）──
    hop = fs // 100
    rms = np.array([np.sqrt((x[i:i + hop] ** 2).mean())
                     for i in range(0, len(x) - hop, hop)])
    voiced = rms > rms.max() * 0.05
    # 填平短于 0.4s 的小间隙
    i = 0
    while i < len(voiced):
        j = i
        while j < len(voiced) and not voiced[j]:
            j += 1
        if 0 < i and j < len(voiced) and j - i < 40:
            voiced[i:j] = True
        i = max(j, i + 1)
    # 平滑速度
    spd = np.convolve(voiced.astype(float), np.ones(20) / 20, mode="same")
    tau = np.concatenate([[0], np.cumsum(spd)]) * 0.01
    T = tau[-1]

    def speech_time(t):
        return float(np.interp(t, np.arange(len(tau)) * 0.01, tau))

    # ── 走位计算 ──
    tag_cues = cues or []

    def random_cues_fn():
        ear = random.choice(["左耳", "右耳"])
        other = "右耳" if ear == "左耳" else "左耳"
        kind = random.choices(
            ["贴耳", "绕过去", "从脑后来", "从面前靠过来"],
            weights=[1, 2, 1, 1],
        )[0]
        if kind == "贴耳":
            return [(0, ear)]
        if kind == "绕过去":
            if T > 6:
                return [(0, ear), (0.2 * T, "脑后"), (0.6 * T, other)]
            return [(0, ear), (0.3 * T, other)]
        return [(0, "脑后" if kind == "从脑后来" else "面前"), (0.35 * T, ear)]

    parsed = [
        (float(speech_time(c["t"])), c["tag"])
        for c in tag_cues
        if c["tag"] in PLACES or c["tag"] in ("贴近", "退开")
    ]
    if not parsed:
        parsed = random_cues_fn()
    parsed.sort(key=lambda c: c[0])

    if parsed[0][1] in PLACES and parsed[0][0] < 0.3:
        start = PLACES[parsed.pop(0)[1]]
    else:
        start = PLACES[random.choice(["左耳", "右耳"])]

    def az_path(a0, a1):
        a0 %= 360
        short = (a1 - a0 + 180) % 360 - 180
        return short if abs(short) <= 150 else a1 - a0

    az_segs, d_segs = [], []

    def track_at(segs, s, init):
        active = [g for g in segs if g[0] <= s]
        if not active:
            return init
        s0, s1, v0, dv = active[-1]
        return v0 + dv * (1 - np.cos(np.pi * min(1.0, (s - s0) / (s1 - s0)))) / 2

    def pos_at(s):
        return (track_at(az_segs, s, start[0]),
                track_at(d_segs, s, start[1]))

    for s, tag in parsed:
        az, d = pos_at(s)
        if tag in PLACES:
            target_az, target_d = PLACES[tag]
            da = az_path(az, target_az)
            nominal = abs(da) / 180 * HALF_TURN if abs(da) > 1 else DIST_MOVE
        else:
            target_d = NEAR if tag == "贴近" else FAR
            da, nominal = 0.0, DIST_MOVE
        dur = max(min(nominal, 0.9 * (T - s)), 0.6 * nominal)
        if abs(da) > 1:
            az_segs.append((s, s + dur, az, da))
        if abs(target_d - d) > 0.01:
            d_segs.append((s, s + dur, d, target_d - d))

    phase = random.uniform(0, 2 * np.pi)

    def position(t):
        az, d = pos_at(speech_time(t))
        return (
            az + DRIFT_DEG * np.sin(2 * np.pi * t / 4.3 + phase)
               + 2 * np.sin(2 * np.pi * t / 1.9 + 2 * phase),
            d + DRIFT_M * np.sin(2 * np.pi * t / 3.7 + 3 * phase),
        )

    # ── HRIR 卷积 ──
    dists, H = _load_hrir(fs)

    def hrir_at(az, d):
        a = int(round(az)) % 360
        d = min(max(d, dists[0]), dists[-1])
        k = min(np.searchsorted(dists, d, side="right") - 1, len(dists) - 2)
        w = (d - dists[k]) / (dists[k + 1] - dists[k])
        return (1 - w) * H[k, a] + w * H[k + 1, a]

    N, Hop = 1024, 512
    win = np.hanning(N + 1)[:N]
    L = H.shape[3]
    out = np.zeros((len(x) + 2 * N + L, 2))
    for s in range(-Hop, len(x), Hop):
        seg = np.zeros(N)
        lo, hi = max(s, 0), min(s + N, len(x))
        seg[lo - s:hi - s] = x[lo:hi]
        h = hrir_at(*position((s + N / 2) / fs))
        for e in (0, 1):
            y = fftconvolve(seg * win, h[e])
            out[s + Hop:s + Hop + len(y), e] += y
    out = out[Hop:Hop + len(x) + L]

    # 响度对齐 + 限幅 -1 dBFS
    rms_ratio = np.sqrt((x ** 2).mean() * 2 / max((out ** 2).mean(), 1e-12))
    out *= rms_ratio
    peak = np.abs(out).max()
    if peak > 0:
        out *= min(1.0, 0.89 / peak)
    wavfile.write(out_wav, fs, np.int16(out * 32767))


# ━━━━━━━━━━━━━━━━━━━━━ 完整流程 ━━━━━━━━━━━━━━━━━━━━━

def process_binaural(
    mono_audio_path: str,
    output_path: str,
    text: str = "",
    spatial_cues: list[dict] | None = None,
    inline_tags: list[dict] | None = None,
) -> str:
    """
    完整的双耳处理流程。

    mono_audio_path: 输入单声道音频（mp3/wav）
    output_path: 输出双耳音频路径（.wav）
    text: 原文（用于内联标签解析或文本锚点对齐）
    spatial_cues: [{"text":"哈哈","tag":"脑后"},...] 文本锚点
    inline_tags: [{"char_pos":N,"tag":"右耳"},...] 来自 parse_inline_tags

    返回输出文件路径。
    """
    import tempfile

    # 确保输入是单声道 WAV
    input_wav = mono_audio_path
    tmp_wav = None
    if not mono_audio_path.lower().endswith(".wav"):
        tmp_wav = tempfile.mktemp(suffix="_mono.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-i", mono_audio_path, "-ac", "1", tmp_wav],
            capture_output=True, check=True,
        )
        input_wav = tmp_wav

    try:
        # 确定走位 cues
        cues = None
        anchors = spatial_cues or inline_tags
        if anchors:
            cues = align_anchors(input_wav, anchors)

        # 渲染
        render_binaural(input_wav, output_path, cues)
    finally:
        if tmp_wav and os.path.exists(tmp_wav):
            os.remove(tmp_wav)

    return output_path
