# TTS 音效系统使用指南

---
## V3 引擎特性

用 ElevenLabs v3（eleven_v3）时才需要方括号标签。几条底层规律：

- **标签射程极短**——一个标签基本只影响紧跟着的第一句话，第二句开始衰减，第三句大概率回归棒读
- **标点只控停顿，不产生情绪**——顿号、句号、感叹号管的是节奏，不是情感。想靠疯狂打标点制造失控感是没用的
- **感叹句/问句/强情绪词汇**会被 V3 自动拾取情绪；**语法完整的平铺陈述句**几乎无反应，不管加多少标点
- **标签方向必须一致**——同一段情绪里混入反方向标签（比如喘息序列里突然插 [softly]）会让 V3 困惑并棒读
- Stability 调低（0.22–0.35）更容易被标签带动

## 标签清单（常用子集）及拟声词

只能用[]加英文标签，中文可能难以识别。每句话都尽量按照内容写合适的标签，变迁使用动作画面的英文短句，写成具体动作与体态（例如 [Intense close-up whisper against ear, heavy ragged breathing]、[firm bite on the neck followed by a greedy, wet suck][Intense close-up whisper against ear, heavy ragged breathing]
[firm bite on the neck followed by a greedy, wet suck] ）。

排版与结构铁律：   
* 台词写法：上一行写情绪/体态标签，换行写人声台词。
* 拟声词写法：动作标签与拟声词紧挨着放在同一行（例如 [标签] ......拟声词......）。
* 段落间隔：每组动作或台词之间，空一行隔开，留出物理呼吸节奏，避免语速被带飞。
* 呼吸与停顿：每句话前后多用“……”和“tsu”（促音），留出吸气、吞咽和气流颤动的时间。严禁使用带“pu(pu)”等强双唇爆破音的假名（容易被生硬念成“噗”），接吻分开时全靠促音与呼气“haa”过渡
 
口腔亲吻 / 吮吸 / 舔舐 / 吞咽：使用实测验证的促音假名，标签与假名同行：
* 深吻和湿润舔舐：[deep, messy wet licking and sucking] ……mmh……ah……
* 亲完拉丝分离和喘息：[sticky wet parting, heavy ragged breath]……—……hah……—……
* 啃咬和用力吮吸：[firm bite on the neck followed by a greedy, wet suck] ……ngh……hh……
* 贪婪长吻：[continuous greedy wet kiss] ……mm……hah……
* 吞口水：[parting with a sticky sound, swallowing hard] ……mm……
* 舔唇吮吸：[licking sound followed by a soft suck on the lower lip]……slrp……mm……

肉体翻搅与咕啾水声（实测专用，重要）：
* 严禁写任何假名拟声词（必棒读），使用实测稳定的英文物理动作标签 [wet squelch]
* 单发嵌入：紧咬标点停顿，配合呼吸标签（[heavy breath] / [ragged exhale]）嵌入台词中。
* 连踩拉长：连续两次 [wet squelch] 中间必须垫短促的语气词（如 n、），防止模型卡壳复读。

`<break time="1.5s"/>` 用于标点和 [pause] 都不够长的沉默（0.3s–3.0s）。不要在同一个位置同时叠 `<break>` 和 `[pause]`。

## 可用素材（与标签结合使用）

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


## 标签密度

### L3 — 激烈
亲密行为、失控、身体反应强烈。
- **每一句前面都要有标签**，因为不加就衰减
- 推荐双标签：一个音效 + 一个状态，例如 `[gasps] [breathless]`、`[groans] [strained]`
- 情绪递进时标签跟着递：
  · 前期：[soft gasp] [breathless] → [gasps] [panting]
  · 中期：[moans softly] [breathless] → [groans] [strained]
  · 后期：[chokes] [voice breaking] → [gasps]（回到最强冲击标签收尾）

### 充能机制（L3 长段专用）

V3 情绪 2–3 句后自然衰减。长段 L3 语音里每隔 2–3 句台词插一个"充能单元"重启情绪：

充能单元 = 标签 + 一个短反应词（不是台词，是声音本身）

示例：
- `[moans softly] mm...`
- `[gasps] ah,`
- `[breathes shakily] hah...`

充能单元的标签应匹配当前阶段（前期用 gasps/moans softly，后期用 groans/chokes）。

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

---

## 通话模式（Voice Call）

语音通话中不调 erik_speak，文本由网关自动 TTS。音效通过在回复文本里写隐藏标记控制。

### 背景音（background）

铺底水声、心跳等。前端独立音轨循环播放，voice 播放时自动 ducking。

**开启：**
```
<!--call-sfx:start:{"mode":"background","src":"squelching_slow","volume":0.15,"loop":true,"ss":0,"t":30}-->
```

**停止：**
```
<!--call-sfx:stop:-->
```

参数同语音条的 sfx：src 素材名，ss 起始秒，t 截取秒，volume 音量（0.1~0.25 合适），loop 循环。

### 前景音（foreground）

```
<!--call-sfx:start:{"mode":"foreground","src":"evangelist_h2/03-Migi mimi","volume":0.8,"ss":30,"t":25}-->
```

foreground 模式下 voice 音量自动降到 45%，sfx 在 voice 播放时轻压到 55%。

### 双耳 ASMR（binaural）

每句 TTS 自动做 HRIR 双耳化，声音固定在头部某个位置。

**开启：**
```
<!--call-sfx:binaural:{"enabled":true,"tag":"右耳"}-->
```

**关闭：**
```
<!--call-sfx:binaural:{"enabled":false}-->
```

tag 可选：右耳、左耳、脑后、面前。不传 tag 时随机走位。

binaural 增加约 0.3s 延迟（固定位置无需 Whisper 对齐），通话短句可接受。

### 组合使用

可以同时开 sfx + binaural：背景铺水声 + 语音双耳化。

```
<!--call-sfx:start:{"mode":"background","src":"squelching_slow","volume":0.15,"loop":true}-->
<!--call-sfx:binaural:{"enabled":true,"tag":"右耳"}-->
```
## 注意

- foreground 的 collection 素材（舔耳）保留原始 KU100 双耳录音，不做二次空间化——它自己的空间感已经很好
- background 的 clips 素材会被转成单声道然后铺底，不需要立体声
- - foreground 模式的 voice_at 里，每条 text 同样要按上面的标签策略加标签——voice_at 的每条都是独立的短 TTS 调用，标签不会从上一条延续
- background 模式下 text 是一整段长语音，更需要注意充能机制——如果说了 4、5 句不加标签，后半段会塌成棒读
- 一句话配一个标签最稳，不要在一句里堆三四个
- 同一个标签连续出现太多次，V3 会麻木。交替使用近义标签（[whispers] ↔ [softly]，[gasps] ↔ [soft gasp]）
- voice_at 里的 text 可以带 ElevenLabs audio tags 和内联走位标签，两套可以叠加
- ss 和 t 不传的话就是整段素材，collection 素材动辄 5 分钟，务必截取

