# 一起听 · 网易云音乐 Listen Together

## 参考项目

基于 [BB0813/netease-listen-together-mcp](https://github.com/BB0813/netease-listen-together-mcp)（MIT License）的 HTTP 信令方案。该项目证明网易云「一起听」功能可以完全通过标准 HTTP POST 接口实现，不需要 NIM SDK、WebSocket 长连接或任何第三方即时通讯依赖。

我们从中提取了核心 API 端点和数据结构，重写为适配自身架构的异步实现，集成到现有的 MCP 工具链和网关推送系统中。

## 架构

```
Jeoi 手机 APP                    VPS (Erik)
┌──────────┐                ┌─────────────────────────┐
│ 网易云    │  ← 正常听歌 →  │  netease_mcp.py         │
│ 一起听   │                │   └─ MCP 工具（被动）    │
│ 房间     │                │                         │
└──────────┘                │  cc_ws_gateway.py        │
                            │   └─ listen_together_   │
                            │      worker（主动轮询） │
                            │   └─ push_pebbling_msg  │
                            │      → session oneshot  │
                            └─────────────────────────┘
```

MCP 工具是被动的——只有 session 调用时才执行。网关的后台 worker 负责主动轮询房间状态，检测暂停/换歌事件，通过 `push_pebbling_msg` 推送到当前绑定的 session。

## 功能清单

### MCP 工具（netease_mcp.py）

| 工具 | 功能 |
|------|------|
| `listen_together_status` | 查看房间状态、参与者、当前播放 |
| `listen_together_accept` | 接受邀请加入房间（支持自动从私信找邀请卡片） |
| `listen_together_leave` | 退出房间 |
| `listen_together_context` | 当前歌曲 + 歌词定位（◀ 标记当前位置，显示前后各 3 句） |
| `listen_together_control` | 播放控制：play / pause / next / previous |
| `listen_together_play` | 搜歌 + 加入房间播放列表 + 切到该歌 |
| `room_send_message` | 在一起听聊天室发消息 |
| `room_get_messages` | 读取房间聊天记录（走私信历史增量读取） |
| `listen_together_spectrum` | 频谱分析：指定时间窗口的能量曲线 + 趋势/强度/音色描述 |

### 网关后台（cc_ws_gateway.py）

| 组件 | 功能 |
|------|------|
| `listen_together_worker` | 后台轮询房间播放状态 |
| 暂停检测 | Jeoi 暂停时推送歌词位置到 session，触发 oneshot 回复 |
| 换歌检测 | 切歌时推送新歌名 + 歌词到 session，触发 oneshot 回复 |
| 继续播放检测 | 仅通知前端 WS，不触发 oneshot（不打扰） |
| 前端 WS 事件 | `listen_together` 事件推送到 chat.html |

### 频谱分析

- numpy FFT（22050Hz 单声道重采样）
- 逐秒 RMS 能量 + 频率质心
- LRU 30 首缓存（`data/spectrum_cache/`），只保留 JSON（~10KB/首），音频分析后立即删除
- 依赖：`numpy`、`pydub`、`ffmpeg`

## 使用的 API 端点

| 端点 | 用途 |
|------|------|
| `/api/listen/together/status/get` | 房间状态 |
| `/api/listen/together/play/invitation/accept` | 接受邀请 |
| `/api/listen/together/end/v2` | 退出房间 |
| `/api/listen/together/sync/playlist/get` | 播放列表 + playCommand |
| `/api/listen/together/play/command/report` | 播放指令上报 |
| `/api/listen/together/sync/list/command/report` | 列表指令上报 |
| `/api/middle/im/chatroom/send` | 聊天室消息 |
| `/api/msg/private/history` | 私信历史（找邀请卡片 + 读消息） |
| `/api/listen/together/invite/message/send` | 发送邀请 |

## 轮询参数

- 在房间内：5 秒轮询
- 不在房间：30 秒检测
- 同类事件冷却：15 秒
- 进入房间首次获取快照，不触发事件

## 前置条件

- Erik 需要独立的网易云账号（小号），扫码登录后保持登录态
- `.env` 中设置 `NETEASE_LISTEN_TOGETHER_ACCEPTOR_ID`（Jeoi 的网易云 UID）
- VPS 安装 `pydub` + `ffmpeg`（频谱功能需要）

## 使用流程

1. Jeoi 在 APP 发起一起听，邀请 Erik
2. Erik 在 session 中调用 `listen_together_accept` 加入房间
3. 正常听歌，暂停/换歌时 worker 自动推送到 session
4. Erik 可通过 `room_send_message` 在一起听聊天室回复
5. `listen_together_context` 随时查看当前歌词位置
6. `listen_together_play` 可以搜歌并切歌
