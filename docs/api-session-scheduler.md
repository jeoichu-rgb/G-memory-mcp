# API Session 保活与静默调度器

API 通道使用 OpenRouter/Claude Code 时的统一后台调度。Subscription 通道保持原有行为。

## 两层 Session 绑定

API 调度器同时维护两种不同用途的绑定：

| 字段 | 用途 |
|---|---|
| `gateway_session_id` → `cc_session_id` | 决定 desire / pebbling 应注入哪个 Claude Code transcript |
| `logical_session_id` → `sticky_session_id` | 决定 OpenRouter 请求应回到哪个 provider 节点，以复用 prompt cache |

Forge 会创建新的 gateway session 和 Claude transcript，但 `inherited_from` 链的根不变。因此同一段逻辑聊天的 `sticky_session_id` 不变；底层 `cc_session_id` 可以更新。这不会产生两套互相竞争的路由缓存。

只有真实 API 消息使用成功后才激活调度器。仅在前端选中 API 模型但没有发消息，不启动。切回 subscription 并发送真实消息时暂停 API 调度器。进程重启后也暂停，直到新的真实 API 请求重新提供规范化缓存前缀。

## 统一 50 分钟时钟

`last_api_request_at` 由本地 OpenRouter 代理在每一次成功 API 请求开始时更新，包括：

- 真实聊天及其工具子轮；
- patrol；
- desire；
- pebbling；
- 纯 cache keepalive。

后台每 30 秒检查，但只有距最近成功 API 请求至少 3000 秒时才允许领取一个槽位。Patrol 的 5/10/20 分钟逻辑不改；patrol 调用本身会把 50 分钟时钟向后移动。

## 槽位优先级

严格顺序：

1. 纯 cache keepalive；
2. desire；
3. pebbling。

纯 keepalive 成功时，本槽不再发 desire 或 pebbling。若 desire 已到期但没有取得槽位，则调用现有 `partial_satisfy`：继续走多级阈值，attachment/libido 第三次仍未满足时按现有逻辑完整回落并从头计算。Pebbling 不记为已执行，继续等待。

纯 keepalive 连续 2 次成功请求却 `cache_read_input_tokens == 0` 时熔断。传输失败或 HTTP 失败不计 cache miss，也不在同一次检查里追加第二个付费调用。熔断在下一条真实 API 用户消息到来时复位。

当前不设置单次费用、completion token 或每日调用的硬上限；usage 仅记录和展示，不因超额主动制造失败。

## Keepalive 请求

Keepalive 由 OpenRouter 代理直接发送，不经过 `run_cc_oneshot()`，因此：

- 不写入 Claude Code transcript；
- 不进入聊天历史、摘要、Forge 或长期记忆；
- 只重放到最后一个显式 cache breakpoint 为止的稳定前缀；
- 保留原请求的 model、tools、thinking/effort、provider 和 TTL；纯 keepalive 将输出硬上限设为 20，desire 的每次 API 请求设为 800；
- 不发送 `max_completion_tokens=0`；
- 旧式 `thinking.enabled + budget_tokens` 与小上限冲突时跳过限额并写日志，避免 HTTP 400；
- 若缓存尾部包含未闭合的 `tool_use`，在 breakpoint 后补合成 `tool_result`，不修改缓存前缀。

返回文本不进入聊天，只保留最多 200 字符的诊断预览。状态可从 `GET /api/pebbling/status` 的 `api_scheduler` 查看。

## Terminal 排障命令

在 VPS 仓库目录 `/opt/G-memory-mcp` 执行：

```bash
# 持久化状态：绑定、下一次到期、最近命中、miss 数和 breaker
python3 api_scheduler_diag.py status

# 查询正在运行的 gateway；额外显示内存里的 snapshot_ready
python3 api_scheduler_diag.py live

# 最近 30 条完整调度日记
python3 api_scheduler_diag.py tail

# 最近 100 条
python3 api_scheduler_diag.py tail -n 100

# 只看 404、cache miss、熔断和其他 warning/error
python3 api_scheduler_diag.py errors -n 100

# 实时追踪全部事件；Ctrl-C 退出
python3 api_scheduler_diag.py follow

# 实时只看错误/miss/熔断
python3 api_scheduler_diag.py follow --errors
```

结构化日记默认写在：

```text
/opt/G-memory-mcp/logs/api_scheduler.jsonl
```

每行是一个独立 JSON，终端脚本只负责格式化读取；也可以直接：

```bash
tail -f logs/api_scheduler.jsonl
grep '404\|HTTP_ERROR\|CACHE_MISS\|BREAKER' logs/api_scheduler.jsonl
```

如部署目录或 gateway 端口不同，可覆盖：

```bash
CC_CWD=/custom/path python3 api_scheduler_diag.py status
API_SCHEDULER_STATUS_URL=http://127.0.0.1:3000/api/pebbling/status \
  python3 api_scheduler_diag.py live
```

### 日记标记

| `code` | 含义 |
|---|---|
| `API_SESSION_BIND` | 真实 API 对话或 Forge 完成后绑定逻辑 session / sticky session |
| `API_SESSION_SUSPEND` | 用户切回 subscription，API 调度暂停 |
| `OR_PROXY_OK` | Claude Code → 本地代理 → OpenRouter 请求收到成功状态码 |
| `OR_PROXY_HTTP_ERROR` | OpenRouter 代理请求返回 4xx/5xx；404 会在这里看到 endpoint、request id 和 breakpoint 数 |
| `OR_PROXY_TRANSPORT_ERROR` | DNS、连接、TLS、超时等尚未收到 HTTP 状态的错误 |
| `SCHEDULER_SLOT` | 50 分钟槽被领取，记录候选和最终选择 |
| `API_OUTPUT_CAP` | 记录 keepalive/desire 请求的目标上限、实际上限，以及旧式 thinking 防护是否跳过限额 |
| `API_THINKING_MODE` | 新建/Forge API session 将固定 budget 迁移为 adaptive，并保留当前 effort（含 max） |
| `API_JOB_RESULT` | desire 的停止原因、可见字符数、工具和最多 200 字符预览 |
| `KEEPALIVE_HIT` | 纯保活成功且真实读取到 cached tokens |
| `KEEPALIVE_CACHE_MISS` | HTTP 成功但 cache read 为 0；计入连续 miss |

旧 API session 不在半途改写 thinking 参数；新建或 Forge 后的 API session 才切换到 `adaptive`，并继承原先选择的 effort（包括 `max`）。边界上的第一次请求可能因参数变化重建一次缓存，之后聊天和保活继续共用同一组参数。
| `KEEPALIVE_HTTP_ERROR` | 纯保活返回 4xx/5xx，附最多 300 字符响应；不计 cache miss |
| `KEEPALIVE_TRANSPORT_ERROR` | 纯保活在收到 HTTP 响应前失败；不计 cache miss |
| `KEEPALIVE_BREAKER_OPEN` | 连续两次真实 cache miss，纯保活熔断 |
| `DESIRE_ROLLBACK_FOR_KEEPALIVE` | keepalive 抢到槽，到期 desire 按现有 partial/三级逻辑回落 |
| `DESIRE_RUN_OK` / `DESIRE_RUN_EMPTY` | 熔断后 desire 获得槽位的结果 |
| `PEBBLING_RUN` | keepalive 熔断且无 desire 时 pebbling 获得槽位 |
| `*_SESSION_NOT_READY` | 语义任务绑定的 transcript 与当前 tmux 不一致，因此没有误发 |

日志不得写入 API key、Authorization header、完整 prompt 或完整回复；输出预览最多 200 字符，sticky id 是逻辑 session 的不可逆哈希。

### 常见排障

**看到 404：**

先运行 `errors`，区分 `OR_PROXY_HTTP_ERROR` 与 `KEEPALIVE_HTTP_ERROR`。前者表示 Claude Code 的正常请求经过代理时 endpoint/参数失败；后者只发生在纯保活直连。重点检查 `upstream_path`、OpenRouter base URL 和是否误加了 `max_completion_tokens=0`。本实现不会主动写入该参数。

**`KEEPALIVE_CACHE_MISS`：**

HTTP 请求本身成功，但没有读到缓存。第一次迁移/冷写可以正常出现；连续第二次会看到 `KEEPALIVE_BREAKER_OPEN`。检查 sticky id 是否变化、model/tools/thinking 是否变化、前缀是否达到模型最低缓存门槛，以及 provider 是否发生 fallback。

**`status` 显示 active，但 `live` 的 `snapshot_ready=false`：**

进程刚重启或尚未收到一条成功的真实 API 请求。先在该 API session 发一条真实消息；只有代理捕获规范化 payload 后才能安全构造 keepalive。

**一直只有 keepalive，没有 desire/pebbling：**

这是当前明确设定的优先级。只要 keepalive 持续命中，它永远优先；到期 desire 会留下 `DESIRE_ROLLBACK_FOR_KEEPALIVE` 并按现有层级回落。只有 keepalive 连续 miss 熔断后，desire/pebbling 才会取得槽位。

## Desire 的 API 行为

- 被动/主动开关只控制能否开独立主动轮，不再改变 drift cap。
- 一条用户消息可以同时推动多个关键词维度；总原始脉冲仍限制为一次正常 pulse 的预算。
- 对话 desire 只在模型成功回复后 satisfy；失败保留 pending。
- UTC+8 00:30–08:30 冻结静默 libido drift，并阻止 libido 静默主动轮或 pebbling 覆写；真实亲密对话照常 pulse 和注入，不补算夜间漂移。

## 必须保持的缓存约束

1. 同一逻辑聊天保持同一 OpenRouter `session_id` 和 API key。
2. Model、provider、tools、system、thinking/effort 和 cache breakpoint 必须与真实聊天一致。
3. 不设置 `provider.order` / `provider.sort`，避免绕过 sticky routing。
4. 动态时间、desire 水位和临时事件必须位于 cache breakpoint 之后。
5. `cache_control` 只表示允许缓存；真实命中以返回 usage 为准。
6. 前端定时器不可靠，调度只在后端 worker 运行。
7. 只保活最近一个真实使用的 API session。
8. 用户消息、tmux 发送和 API scheduler 使用同一生成锁；同一槽位另有原子领取锁。
9. 迁移或进程重启后的第一次 cache miss 是正常冷写；连续 miss 才熔断。
10. Opus 4.6 的短前缀可能低于 provider 最低缓存门槛，请通过 usage 判断，不以 HTTP 成功推断命中。
