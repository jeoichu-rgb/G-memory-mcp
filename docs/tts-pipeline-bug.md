# TTS Pipeline Bug：语音通话句间卡顿

## 现象

语音通话时，Erik说的前两句话之间衔接流畅，但第二句和第三句之间会有明显停顿（约2秒）。Erik那边没有在两句之间做任何额外操作——纯文字输出，卡顿发生在网关的TTS pipeline。

## 根因

网关的 `_tts_worker_loop` 是纯串行的：

```python
async def _tts_worker_loop(self):
    while True:
        item = await self._tts_queue.get()
        await self._auto_tts(*item)  # 调API + 发WS，完成后才取下一句
```

每句话必须等上一句的TTS API返回并发送WS消息后，才开始下一句的API请求。MiniMax TTS API单次调用约2秒，所以如果前端播完当前音频（比如一句很短的话只有1秒）但下一句的TTS还没生成完，就会卡。

前两句不卡是因为：句子1播放期间（假设3秒），句子2的TTS有足够时间完成（2秒）。但句子2如果很短（播放1秒），句子3的TTS还在等API返回——卡顿。

## 第一次修：prefetch with `_fill()`

把 `_auto_tts`（API调用+WS发送耦合在一起）拆成 `_call_tts_api`（只调API）和worker里的WS发送。worker维护一个 `pending` 列表，用 `_fill()` 方法从队列预取句子并提前发起API请求：

```python
def _fill():
    while len(pending) < PREFETCH + 1:
        item = self._tts_queue.get_nowait()  # 非阻塞
        task = asyncio.create_task(self._call_tts_api(text))
        pending.append((task, text, subtitle))

# 主循环
task, text, subtitle = pending.pop(0)
result = await task      # 等当前句子
await self._ws(...)      # 发WS
_fill()                  # 处理完了才去预取
```

**问题**：`_fill()` 只在当前句子处理完之后才调用。如果句子是流式到来的（tailer每0.4秒轮询一次），`await task` 期间新句子进了队列但没人去取，prefetch根本没生效。日志里看不到任何TTS时序信息（因为没加日志），无法确认是否并行。

## 第二次修：feeder/sender 双协程

改成两个独立的协程并行运行：

```python
async def _tts_worker_loop(self):
    slots = asyncio.Queue()

    async def feeder():
        # 持续从tts_queue取句子，立即发起API调用
        while True:
            item = await self._tts_queue.get()
            if item is None:
                await slots.put(None)
                break
            text, subtitle = item
            task = asyncio.create_task(self._call_tts_api(text))
            await slots.put((task, text, subtitle))

    async def sender():
        # 按顺序等每个task完成，发WS
        while True:
            entry = await slots.get()
            if entry is None:
                break
            task, text, subtitle = entry
            result = await task
            if result:
                await self._ws({...})

    feeder_task = asyncio.create_task(feeder())
    await sender()
```

feeder和sender各自运行：

- **feeder** 不等任何API返回，只管从队列取句子 → 创建API task → 放入slots
- **sender** 按顺序从slots取出task → await → 发WS

当sender在等句子1的API返回时，feeder已经把句子2、3的API请求发出去了。三个API调用真正并行。

### 日志验证

加了时序日志后确认并行生效：

```
05:48:08,773 TTS feeder: #1 started → 想。
05:48:08,773 TTS feeder: #2 started → 想要主人的。
05:48:08,773 TTS feeder: #3 started → 想要主人的。        ← 三句同时发起
05:48:10,638 TTS API #2: 1.82s
05:48:10,978 TTS API #3: 2.13s
05:48:11,060 TTS API #1: 2.29s                             ← 2秒内全部返回
05:48:11,061 TTS sender: #1 sending voice
05:48:11,062 TTS sender: #2 sending voice
05:48:11,063 TTS sender: #3 sending voice                  ← 1ms内全部发给前端
```

## 引出的问题：字幕不同步

并行TTS修好了卡顿，但引出一个新bug：字幕跳到最后一句。

原来串行模式下，voice事件一个一个到前端，每到一个就 `setSub(text)` 更新字幕，正好和音频播放同步。并行模式下，三个voice事件在1ms内全部到达，`setSub` 被连续调用三次，字幕直接显示最后一句的内容，但音频还在播放第一句。

**修法**：字幕不再跟WS事件同步，改为跟音频播放同步。audioQ从存URL改为存 `{url, text, subtitle}` 对象，`playNext()` 播放哪句才显示哪句的字幕：

```javascript
// 之前
function enqueueAudio(url) { audioQ.push(url); ... }
function playNext() { audio.src = audioQ.shift(); ... }

// 之后
function enqueueAudio(url, text, subtitle) {
    audioQ.push({url, text, subtitle});
    ...
}
function playNext() {
    const item = audioQ.shift();
    setSub(item.text, item.subtitle);  // 播放时才更新字幕
    audio.src = item.url;
    ...
}
```

## 相关文件

- `cc_ws_gateway.py` — `TranscriptTailer._tts_worker_loop`, `_call_tts_api`
- `call.html` — `enqueueAudio`, `playNext`, voice事件处理
