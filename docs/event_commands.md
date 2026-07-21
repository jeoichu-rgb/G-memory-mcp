# 挂载事件（Event）命令

全部通过 `palace(cmd, data)` 调用，不是独立工具。

| cmd | 说明 | data |
|-----|------|------|
| `event_create` | 创建事件窗口 | `{name}` |
| `event_post` | 往事件写一条状态更新 | `{event(slug), content}` |
| `event_edit` | 编辑某条更新 | `{event, entry_id, content}` |
| `event_rm` | 删除某条更新 | `{event, entry_id}` |
| `event_list` | 列出事件全部更新（倒序） | `{event, latest(可选,限制条数)}` |
| `event_drop` | 删除整个事件 | `{event}` |
| `event_ls` | 列出所有事件名 | 无 |

数据源：gateway 的 `/opt/G-memory-mcp/event_store.json`，palace 通过 HTTP 读写（`GET/PUT /api/event-store`），前端通过 WS 读写。三方共享同一份数据。

前端入口：设置面板 → 挂载事件。
