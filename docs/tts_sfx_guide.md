# TTS 音效系统使用指南

> 仅在使用 erik_speak + sfx 时参照。纯语音（无素材）看拟声词指南即可。

## 可用素材

### Collections（双耳录音，foreground 保留原始立体声）

| 名称 | src 写法 | 标签 | 说明 |
|------|---------|------|------|
| 右耳舔耳 | `evangelist_h2/03-Migi mimi` | ear_right, lick | ~295s KU100 右耳 |
| 左耳舔耳 | `evangelist_h2/04-Hidari mimi` | ear_left, lick | ~309s KU100 左耳 |

### Clips（单独短素材）

| 名称 | src 写法 | 标签 | 说明 |
|------|---------|------|------|
| 亲吻声 | `log kiss` | kiss | 长段亲吻，可截段循环 |
| 口交水声 | `blowjob` | wet, oral | 吮吸湿润声 |
| 水声·快 | `squelching_fast` | wet, thrust | 快节奏，适合铺底 |
| 水声·慢 | `squelching_slow` | wet, thrust | 慢节奏，适合铺底 |
| 心跳 | `heartbeat` | ambient | 低频心跳，纯背景用 |
| 拍打 | `slap` | impact | 极短，叠入用 |

---

## 三种模式

### 1. 纯语音（不传 sfx_mode）

跟以前一样。text 里写标签和台词，binaural=True 做空间化。ElevenLabs audio tags（[whispers] [sighs] 等）直接写在 text 里。

### 2. foreground — 素材为主

**场景**：舔耳、亲吻——素材是主角，我偶尔插一句话。

```
erik_speak(
    text="",
    backend="elevenlabs",
    binaural=True,
    sfx_mode="foreground",
    sfx='{"src":"evangelist_h2/03-Migi mimi","ss":30,"t":25,"volume":1.0}',
    voice_at='[{"t":5,"text":"[右耳][whispers] 别动"},{"t":18,"text":"[右耳] 乖"}]'
)
```

**关键参数**：
- `sfx.src`：素材名（见上表）
- `sfx.ss`：从素材的第几秒开始截取
- `sfx.t`：截取多少秒
- `sfx.volume`：素材音量 0~1（默认1.0）
- `sfx.voice_volume`：叠入语音的音量 0~1（默认0.85）
- `sfx.loop`：true 时循环播放截取片段
- `sfx.duration`：loop=true 时的总时长（秒）
- `voice_at`：语音插入时间点。t=秒数，text=要说的话（可带内联标签）

**选 ss 的技巧**：collection 素材前几秒通常有说话声或准备音，跳过。舔耳素材 ss=30~60 开始比较纯净。

### 3. background — 语音为主

**场景**：我一直在说话，底下铺着水声/心跳。

```
erik_speak(
    text="[右耳][whispers] ……もう、我慢しないよ。",
    backend="elevenlabs",
    binaural=True,
    sfx_mode="background",
    sfx='{"src":"squelching_slow","volume":0.15,"loop":true}'
)
```

**关键参数**：
- text 正常写，是完整的语音内容
- `sfx.volume`：铺底音量，0.1~0.25 比较合适，太大会盖过人声
- `sfx.loop`：默认 true，自动循环到跟语音一样长
- 不需要 voice_at（text 本身就是完整语音）

---

## 典型搭配

| 场景 | 模式 | 素材 | 说明 |
|------|------|------|------|
| 舔右耳 | foreground | `evangelist_h2/03-Migi mimi` | 素材双耳保留，偶尔叠入耳语 |
| 舔左耳 | foreground | `evangelist_h2/04-Hidari mimi` | 同上 |
| 亲吻 | foreground | `log kiss` | 截一段干净的循环 |
| 做的时候说话 | background | `squelching_slow` 或 `squelching_fast` | 水声铺底 0.15~0.2 音量 |
| 贴着胸口说话 | background | `heartbeat` | 心跳铺底 0.2 音量 |
| 打屁股 | 纯语音 + 单独一条 foreground | `slap` | slap 素材极短，做单独音效叠入 |

## 注意

- foreground 的 collection 素材（舔耳）保留原始 KU100 双耳录音，不做二次空间化——它自己的空间感已经很好
- background 的 clips 素材会被转成单声道然后铺底，不需要立体声
- voice_at 里的 text 可以带 ElevenLabs audio tags 和内联走位标签，两套可以叠加
- ss 和 t 不传的话就是整段素材，collection 素材动辄 5 分钟，务必截取
