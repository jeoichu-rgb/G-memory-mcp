# TTS 音效系统使用指南
## V3 引擎底层规律

- 标签射程极短——只影响紧跟的第一句，第二句衰减，第三句回归棒读
- 标点只控停顿，不产生情绪；感叹句/问句/强情绪词汇会被自动拾取
- 标签方向必须一致——混入反方向标签会让 V3 困惑并棒读
- Stability 调低（0.22–0.35）更容易被标签带动
- 将音频标签单独成行或紧跟在句子前面都能被正常识别，配合换行与省略号也能进一步增强语速放慢和停顿的舒缓效果。
- 一句话配一个标签最稳；同标签连续太多次会麻木，交替近义标签（[whispers] ↔ [softly]）

## 排版

- 拟声词：标签与拟声词同一行（`[标签] ……拟声词……`）
- 段落间空一行，留物理呼吸节奏
- 每句前后多用"……"和促音"tsu"留气流时间。严禁"pu"等强双唇爆破音，接吻分开靠促音+"haa"过渡
- 停顿用 [pause] 和 [long pause]，省略号 …… 做气息节拍

## 口腔拟声词（标签+假名同行）

| 动作 | 写法 |
|------|------|
| 深吻/湿润舔舐 | `[deep, messy wet licking and sucking] ……mmh……ah……` |
| 亲完拉丝/喘息 | `[sticky wet parting, heavy ragged breath]……—……hah……—……` |
| 啃咬/用力吮吸 | `[firm bite on the neck followed by a greedy, wet suck] ……ngh……hh……` |
| 贪婪长吻 | `[continuous greedy wet kiss] ……mm……hah……` |
| 吞口水 | `[parting with a sticky sound, swallowing hard] ……mm……` |
| 舔唇吮吸 | `[licking sound followed by a soft suck on the lower lip]……mmh……ah……` |

## 水声标签（严禁假名拟声词）

用 `[wet squelch]`。单发嵌入配呼吸标签（`[heavy breath]`），连续两次中间必须垫短语气词（如"n、"）防止复读。

## 标签密度 L3（激烈场景）

**每句前面都要标签**，推荐双标签（音效+状态）。情绪递进：
- 前期：`[soft gasp] [breathless]` → `[gasps] [panting]`
- 中期：`[moans softly] [breathless]` → `[groans] [strained]`
- 后期：`[chokes] [voice breaking]` → `[gasps]`

**充能机制**：V3 情绪 2–3 句后衰减。每隔 2–3 句插一个充能单元（标签+短反应词）重启情绪：
`[moans softly] mm...` / `[gasps] ah,` / `[breathes shakily] hah...`

## 双耳走位

binaural=True 时用 HRIR 渲染空间位置。不传走位信息会随机跳，必须主动设计。

### 方位

| 标签 | 方位角 | 距离 | 听感 |
|------|--------|------|------|
| `[左耳]`/`[右耳]` | 90°/270° | 25cm | 贴耳，气息感最强 |
| `[面前]` | 0° | 42cm | 面对面居中 |
| `[脑后]` | 180° | 31cm | 背后压迫感 |
| `[贴近]`/`[退开]` | 不变 | 25cm/50cm | 只改距离不转方向 |

### 走位规则

- 位移至少用一句话（或一段气声）滑过去，不要瞬移跳切
- 两三句话换一次位置就够，太密会晕
- 面前是默认状态，贴耳是亲密升级动作——不要一开口就贴耳
- 脑后适合低语/命令/环绕，脑后→耳边是最有叙事感的走位
- 走位全部通过 `spatial_cues` JSON 控制，text 里**不要**写 `[左耳]` 等方括号标签（会干扰情绪识别）

```json
spatial_cues='[
  {"text":"看着我", "tag":"面前"},
  {"text":"就这样", "tag":"贴近"},
  {"text":"别动",   "tag":"右耳"}
]'
```

### 走位模板

| 场景 | 走位 |
|------|------|
| 贴耳 ASMR | `[右耳]` 全程不动 |
| 面对面低语 | `[面前]→[贴近]` |
| 从背后靠近 | `[脑后]→[右耳]` |
| 环绕 | `[右耳]→[脑后]→[左耳]` |
| 拉近又放开 | `[面前]→[贴近]→[退开]` |
| 两侧交替（慎用） | `[右耳]→(3~4句)→[左耳]` |

## 素材库

### Collections（双耳录音，foreground 保留原始立体声）

| src | 标签 | 说明 |
|-----|------|------|
| `evangelist_h2/03-Migi mimi` | ear_right, lick | ~295s 右耳 |
| `evangelist_h2/04-Hidari mimi` | ear_left, lick | ~309s 左耳 |

### Clips

| src | 标签 | 说明 |
|-----|------|------|
| `log kiss` | kiss | 长段亲吻，可截段循环 |
| `blowjob` | wet, oral | 吮吸湿润声 |
| `squelching_fast` | wet, thrust | 快节奏铺底 |
| `squelching_slow` | wet, thrust | 慢节奏铺底 |
| `heartbeat` | ambient | 低频心跳背景 |
| `slap` | impact | 极短叠入 |

## 三种模式

### 1. 纯语音（不传 sfx_mode）

text 写标签+台词，binaural=True 做空间化。

### 2. foreground — 素材为主，偶尔插话

```python
erik_speak(
    text="", backend="elevenlabs", binaural=True,
    sfx_mode="foreground",
    sfx='{"src":"evangelist_h2/03-Migi mimi","ss":30,"t":25,"volume":1.0}',
    voice_at='[{"t":5,"text":"[右耳][whispers] 别动"},{"t":18,"text":"[右耳] 乖"}]'
)
```

### 3. background — 语音为主，底下铺声

```python
erik_speak(
    text="[右耳][whispers] ……もう、我慢しないよ。",
    backend="elevenlabs", binaural=True,
    sfx_mode="background",
    sfx='{"src":"squelching_slow","volume":0.15,"loop":true}'
)
```

### sfx 参数一览

| 参数 | 说明 | 默认 |
|------|------|------|
| `src` | 素材名 | — |
| `ss` | 起始秒（collection 前几秒有杂音，跳过 30~60） | 0 |
| `t` | 截取秒数（collection 动辄 5 分钟，务必截取） | 全长 |
| `volume` | 素材音量 0~1（background 铺底用 0.1~0.25） | 1.0 |
| `voice_volume` | foreground 叠入语音音量 | 0.85 |
| `loop` | 循环（background 默认 true） | false |
| `duration` | loop=true 时总时长 | — |

### 典型搭配

| 场景 | 模式 | 素材 |
|------|------|------|
| 舔右/左耳 | foreground | `evangelist_h2/03` 或 `04` |
| 亲吻 | foreground | `log kiss` |
| 做的时候说话 | background | `squelching_slow/fast` vol 0.15~0.2 |
| 贴着胸口说话 | background | `heartbeat` vol 0.2 |
| 打屁股 | 纯语音 + 单独 foreground | `slap` |

## 通话模式（Voice Call）

通话中不调 erik_speak，音效通过回复文本里的隐藏标记控制。参数同上表。

**背景音：**
```
<!--call-sfx:start:{"mode":"background","src":"squelching_slow","volume":0.15,"loop":true,"ss":0,"t":30}-->
<!--call-sfx:stop:-->
```

**前景音：** `mode` 改 `"foreground"`，voice 自动降到 45%，sfx 播放时轻压到 55%。

**双耳 ASMR：**
```
<!--call-sfx:binaural:{"enabled":true,"tag":"右耳"}-->
<!--call-sfx:binaural:{"enabled":false}-->
```
tag 可选：右耳、左耳、脑后、面前。不传 tag 随机走位。增加约 0.3s 延迟。可同时开 sfx + binaural。

## 注意事项

- collection 素材（舔耳）保留原始 KU100 双耳，不做二次空间化
- clips 素材转单声道铺底
- foreground 的 voice_at 每条是独立短 TTS，标签不从上一条延续，每条都要加标签
- background 的 text 是一整段长语音，更需要充能机制——4、5 句不加标签后半段会棒读
