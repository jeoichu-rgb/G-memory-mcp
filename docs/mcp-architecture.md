# MCP 架构与踩坑记录

> 最后更新：2026-06-07
> 状态：持续更新中

---

## 当前架构

```
Jeoi (浏览器)
  ↓ WebSocket
cc_ws_gateway.py (VPS, port 3000)
  ↓ 每条消息 spawn 一次 CC CLI
claude --output-format stream-json --model ... --resume <session_id> -- "message"
  ↓ CC CLI 启动时连接 MCP
  ├── palace (记忆宫殿)  ← http://127.0.0.1:8001/mcp/Jeoi2026/sse
  └── coreading (共读)   ← https://read.erikssheep.uk/sse?token=Jeoi2026
```

**关键参数：**
- `stdin=DEVNULL`：CC CLI 在交互模式下运行（不加 `--print`），用订阅额度而非独立信用
- `--resume session_id`：恢复对话历史，保持上下文连续
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`：减少不必要的网络请求
- `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`：渐进式压缩（30% → 40% → 50%）

**MCP 配置写入位置（gateway 启动时自动写入三个文件）：**
- `/opt/G-memory-mcp/.claude/settings.json` — 项目级
- `/opt/G-memory-mcp/.claude/settings.local.json` — 本地级
- `~/.claude/settings.json` — 全局级

实际上 CC CLI **不从这些文件读取 MCP 服务器定义**（详见踩坑 #1）。MCP 来源只有云同步和 `claude mcp add`。

---

## Prompt Cache 机制

Anthropic 的 prompt cache 是**服务端**的，基于请求前缀匹配，TTL 5分钟。

**缓存前缀 = system prompt + 工具定义 + 消息历史（从头开始的前缀部分）**

缓存稳定的条件：
- 每次 spawn 的工具定义列表相同（MCP 连接结果一致）
- 消息历史前缀不变（`--resume` 恢复相同对话）
- system prompt 不变

**查看缓存命中情况：**
```bash
tail -f /opt/G-memory-mcp/logs/cc_gateway.log | grep "Result event"
```

关注三个字段：
- `cache_read_input_tokens` — 缓存命中（越大越好）
- `cache_creation_input_tokens` — 新建缓存（首次正常，重复出现说明前缀在变）
- `input_tokens` — 未缓存的裸 token（应该很小）

---

## 踩坑记录

### #1 — CC CLI 不从 settings.json 读取 MCP 服务器

**现象：** 在 `.claude/settings.json`、`settings.local.json`、`~/.claude/settings.json` 中配置了 `mcpServers`，但 CC CLI init 事件显示 `mcp_servers: []`（加了 `--strict-mcp-config`）或只有云同步的服务器（不加）。

**原因：** CC CLI 的 MCP 服务器来源只有：
1. **云同步**（从 claude.ai 账号同步）
2. **`claude mcp add` 手动添加**
3. **`.mcp.json` 项目配置**（需要交互式批准，`stdin=DEVNULL` 下不可用）

`settings.json` 中的 `mcpServers` 字段在 CC CLI 2.1.160 中**不用于加载 MCP 服务器**。它可能用于其他目的（如权限映射），但不会让 CC CLI 连接这些服务器。

**当前方案：** 依赖云同步。Jeoi 的 claude.ai 账号连接了 palace 和 coreading，CC CLI 自动同步使用。

---

### #2 — `--strict-mcp-config` 杀死所有 MCP

**现象：** 加了 `--strict-mcp-config` 后 `mcp_servers` 永远是空数组。

**预期行为：** 只加载本地配置的 MCP，忽略云同步。

**实际行为（CC CLI 2.1.160）：** 不加载任何 MCP 服务器，无论配置在哪个文件。

**结论：** 不能使用此标志。已从所有 spawn 命令中移除。

---

### #3 — Docker IPv6 端口映射导致 SSE 连接重置

**现象：** `curl http://localhost:8001/mcp/Jeoi2026/sse` → `Connection reset by peer`

**原因：** `localhost` 在现代系统上优先解析为 `[::1]`（IPv6）。Docker 的 IPv6 端口映射在转发 SSE 长连接时会重置连接。

**修复：** Palace URL 使用 `127.0.0.1`（强制 IPv4）。Gateway 的 auto-detect 现在优先尝试 `127.0.0.1`。

**验证：**
```bash
# 会失败（IPv6）：
curl http://localhost:8001/mcp/Jeoi2026/sse
# 正常（IPv4）：
curl http://127.0.0.1:8001/mcp/Jeoi2026/sse
```

---

### #4 — Palace health 返回 401 导致 auto-detect 失败

**现象：** Gateway 启动日志 `Palace auto-detect failed, using fallback`。

**原因：** Palace 的 `/health` 端点返回 `401 Unauthorized`。Gateway 原来只认 `200` 状态码。

**修复：** Auto-detect 现在接受任何 HTTP 响应（有响应 = 服务在跑）。

---

### #5 — Persistent CLI 不可行（CC CLI 需要真实终端）

**现象：** PersistentCLI 启动后立即退出，`exit code=1`，stderr 为空。

**原因：** CC CLI 在交互模式下（不加 `--print`）要求 stdin 和 stdout 都是真实终端（TTY）。即使用 `pty.openpty()` 模拟 stdin，stdout 仍然是 PIPE，CC CLI 检测到后退出。

**影响：** 无法保持常驻 CC CLI 进程。每条消息必须 spawn 新进程。

**当前状态：** PersistentCLI 代码保留但默认禁用（`CC_PERSISTENT_CLI=0`）。等 CC CLI 支持 headless 交互模式后可启用。

---

### #6 — `--print` 不能用（计费隔离）

**背景：** Anthropic 2026年6月15日起将 `claude -p`/Agent SDK 用量从订阅额度中分离，使用独立信用计费。

**影响：** Gateway 必须以交互模式运行（不加 `--print`），使用 `-- "message"` 传入消息 + `stdin=DEVNULL`，这样用量计入订阅额度。

---

### #7 — 云同步 MCP 无法通过 CLI 删除

**现象：** `claude mcp remove "claude.ai Gmail"` → `No MCP servers are configured`。

**原因：** `claude mcp remove` 只能删除通过 `claude mcp add` 手动添加的本地服务器。云同步的 MCP 只能从 claude.ai 网页端断开。

**影响：** CC CLI 会加载云同步的所有 MCP（包括不需要的 Gmail、Google Drive、Music_XuLe），增加启动延迟和工具定义 token 开销。

**缓解：** 只要这些服务器的连接结果**每次一致**（始终连上或始终失败），缓存前缀就稳定。

---

### #8 — Python 3.9 不支持 `X | None` 类型注解

**现象：** VPS 上 gateway 启动后立即崩溃，无日志输出。

**原因：** 代码中使用了 Python 3.10+ 的 `int | None` 语法，VPS 运行 Python 3.9。

**修复：** 所有 `X | None` 注解替换为无类型声明的普通赋值。

---

## 日志位置

| 日志 | 路径 |
|------|------|
| Gateway 主日志 | `/opt/G-memory-mcp/logs/cc_gateway.log` |
| Palace 容器日志 | `docker logs $(docker ps -q \| head -1)` |
| CC CLI session 文件 | `~/.claude/projects/-opt-G-memory-mcp/` |

**常用诊断命令：**
```bash
# 实时看 gateway 日志
tail -f /opt/G-memory-mcp/logs/cc_gateway.log

# 只看缓存命中
tail -f /opt/G-memory-mcp/logs/cc_gateway.log | grep "Result event"

# 只看错误
tail -f /opt/G-memory-mcp/logs/cc_gateway.log | grep "ERROR"

# 看 MCP 连接状态
claude mcp list

# 看 CC CLI 的原始 init 输出
cd /opt/G-memory-mcp && timeout 30 claude --output-format stream-json --verbose -- "hi" 2>&1 | grep init

# 测试 palace MCP 连接
curl -v http://127.0.0.1:8001/mcp/Jeoi2026/sse --max-time 5

# 看 settings 文件
cat /opt/G-memory-mcp/.claude/settings.json
cat /opt/G-memory-mcp/.claude/settings.local.json
cat ~/.claude/settings.json
```

---

## 部署流程

```bash
cd /opt/G-memory-mcp && git pull && pkill -f cc_ws_gateway; sleep 1; nohup python3 cc_ws_gateway.py > /dev/null 2>&1 &
```

部署后验证：
```bash
# 确认进程在跑
pgrep -f cc_ws_gateway

# 确认端口在监听
ss -tlnp | grep 3000

# 看启动日志
tail -20 /opt/G-memory-mcp/logs/cc_gateway.log
```

---

## 待解决 / 未来可能

- [ ] CC CLI 支持 headless 交互模式后，启用 PersistentCLI（常驻进程，避免每次重连 MCP）
- [ ] 找到只禁止云 MCP 同步、不禁止本地 MCP 的方法
- [ ] 清理不需要的云同步 MCP（从 claude.ai 网页端操作，或等 CC CLI 支持）
- [ ] MCP 工具调用的缓存开销异常——需要更多数据点确认是否是偶发的
