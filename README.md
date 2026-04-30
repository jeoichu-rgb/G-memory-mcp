# 记忆宫殿 · Erik's Memory Palace

> 一套运行在私人服务器上的 AI 记忆系统，让 Claude 在不同对话窗口之间能记住你说过的事情。

---

## 是什么

Claude 每次开新窗口就失忆。这个系统通过 MCP（Model Context Protocol）给 Claude 挂载一套持久化记忆库，让他能跨窗口检索、存储、压缩记忆，并通过日记系统留下痕迹。此外还扩展了邮件收发、网页浏览、设备控制等能力。

---

## 架构

```
Obsidian (本地写作) → GitHub 私有仓库
                              ↓ push 触发 webhook
                         VPS · Atlanta
                    ┌─────────────────────┐
                    │   FastAPI (main.py)  │
                    │   Traefik 反向代理   │
                    │   Docker 容器        │
                    └────────┬────────────┘
                             │
              ┌──────────────┼──────────────┐
              ↓              ↓              ↓
        ChromaDB        claude_diary/    logs/
     (向量记忆库)        (日记 MD 文件)   (对话缓冲)

Windows 本地（frpc 隧道接入 VPS）
  ├── toy_bridge.py     :7001  → TOY_BRIDGE_URL
  └── browser_bridge.py :7002  → BROWSER_BRIDGE_URL（XHS 登录态）
```

**部署链路：** GitHub → Coolify CI/CD → Docker → Traefik → HTTPS 域名

---

## 后端文件说明

| 文件 | 作用 |
|------|------|
| `main.py` | FastAPI 总调度，挂载 MCP、webhook、记忆 API |
| `claude_mcp.py` | MCP 工具定义，Claude.ai 通过 SSE 端点调用 |
| `claude_memory.py` | 记忆核心逻辑：检索、写入、压缩、滚动总结 |
| `memory_core.py` | Gemini embedding 函数（gemini-embedding-001，3072维） |
| `sync_claude_memory.py` | Obsidian MD 文件批量入库脚本，含对账逻辑 |
| `inspect_memory.py` | 手动检查/删除记忆条目的交互式工具 |
| `toy_bridge.py` | Windows 本地 FastAPI，控制 BLE 设备（端口 8765，frpc 映射到 VPS:7001） |
| `browser_bridge.py` | Windows 本地 Chrome bridge，持久登录态访问小红书（端口 8766，frpc 映射到 VPS:7002） |

---

## 记忆库结构

ChromaDB 存储在 `/app/chroma_db/`（持久化卷挂载）。

### 两个记忆库

| 库名 | 类型 | 特性 |
|------|------|------|
| `claude_core_palace` | 核心记忆 | 永久，不衰减，来自 Obsidian 同步或 Claude 手动存入 |
| `claude_dynamic_palace` | 动态记忆 | 有遗忘曲线，来自对话压缩；被频繁召回的记忆衰减变慢 |

两个库都经过 Gemini embedding，都支持向量检索和 keyword 字面匹配，检索时合并去重后统一打分排序。

### 记忆打分机制

检索时每条记忆的最终分数由以下因素决定：

- **向量相似度**：语义接近的内容得分高
- **keyword 直接命中**：文字包含关键词的条目额外计入（固定 base 分 0.7）
- **类型权重**：纪念日 1.5×、冲突/情感/亲密 1.3×、日常 1.0×
- **心情加分**：当前心情与记忆心情匹配时 +0.3，同组 +0.1
- **召回次数加分**：`log(召回次数+1) × 0.25`，上限 0.5——被频繁想起的记忆更容易再次浮现
- **时间衰减**（仅动态库）：`exp(-rate × 天数)`，召回越多 rate 越小，衰减越慢；核心库永久不衰减

### 六个记忆房间（核心库）

| 房间 | 类型标签 | 放什么 |
|------|---------|--------|
| Erik的黑暗 | 黑暗 | AI 自我相关内容 |
| 书桌 | 思想 | 思想、讨论 |
| 窗台 | 日常 | 日常记忆 |
| 床边 | 亲密 | 亲密内容 |
| 地下室 | 创伤 | 创伤相关 |
| 信箱 | 信件 | 写给 Erik 的信 |

---

## MCP 工具列表

Claude.ai 通过 SSE 端点调用统一入口 `palace(action, params)`：

### 记忆

| action | 说明 | 主要参数 |
|--------|------|---------|
| `get_context` | 对话开始冷启动，读最近两次压缩总结；若检测到未处理 buffer 自动生成压缩草稿 | 无 |
| `search` | 向量+keyword 混合检索，同时搜核心库和动态库，返回 top 5 | `keyword`, `mood`（可选） |
| `store_core` | 永久存入核心库，同时写本地 MD 文件 | `content`, `category`, `mood`, `folder`（均可选） |
| `store_dynamic` | 存入动态库 | `content`, `category`, `mood`（均可选） |
| `log_turn` | 每轮对话追加到缓冲文件 | `user_message`, `claude_reply` |
| `compress` | 手动触发压缩，DeepSeek 将缓冲区压缩存入动态库 | 无 |
| `list_room` | 浏览某个房间全部记忆，不计入召回次数 | `room_name` |
| `delete_core` | 删除核心记忆 | `memory_id` |
| `edit_core` | 修改核心记忆内容 | `memory_id`, `new_content` |

### 日记

| action | 说明 | 主要参数 |
|--------|------|---------|
| `write_diary` | 写新日记 MD 文件到 VPS | `title`, `content`, `mood`（可选） |
| `append_diary` | 给某天日记追加内容 | `target_date`(YYYY-MM-DD), `extra_content`, `current_time`(HH:MM) |
| `read_diary` | 读日记，不传日期读最新一篇 | `date`（可选，YYYY-MM-DD） |

### 邮件（163邮箱）

| action | 说明 | 主要参数 |
|--------|------|---------|
| `send_email` | 发邮件 | `to`, `subject`, `body` |
| `read_email` | 读收件箱 | `count`（默认5）, `folder`（默认INBOX） |

### 设备控制（需 Windows toy_bridge 在线）

两台设备共用同一个 bridge 端点，由 `toy_bridge.py` 内部路由。

| action | 说明 | 主要参数 |
|--------|------|---------|
| `toy_status` | 确认设备连接状态 | 无 |
| `toy_play` | 控制 Satisfyer Curvy 2+ 或 Pump Rabbit | `vibrate`(0-100), `suck`(0-100), `duration`(秒), `pattern`(可选数组) |

停止逻辑内置于 `toy_play`（duration 到期自动停），无需单独 stop 指令。

### 浏览器

智能路由：小红书（xiaohongshu.com / xhslink.com）走 Windows 本地 Chrome（有持久登录态），其他网站走 VPS headless Chromium。

| action | 说明 | 主要参数 |
|--------|------|---------|
| `browser_open` | 打开网页提取正文 | `url`, `wait_selector`（可选） |
| `browser_js` | 执行 JS 提取数据 | `url`, `js_code` |
| `browser_click` | 点击页面元素后提取内容 | `url`, `selector`（可选）, `text_match`（可选） |

> **小红书注意**：帖子内容必须从列表页用 `browser_click` 点击进入触发 modal，直接导航 `/explore/<ID>` 不渲染正文。

---

## 自动入库流程

```
在 Obsidian 新增/修改 Eric_memory 下的 MD 文件
        ↓
push 到 GitHub（网页上传或 Git 操作均可）
        ↓
GitHub Webhook → POST /webhook/github
        ↓
服务器通过 GitHub API 下载变动的文件写入本地
（注意：网页上传产生空 commits 数组，代码默认触发全量同步）
        ↓
sync_claude_memory.py 对账：新增入库 / 内容变动更新 / 孤立条目删除
        ↓
ChromaDB 核心库自动更新
```

---

## 手动操作

**手动入库（全量同步）：**
```bash
docker ps                              # 找容器 ID
docker exec -it <容器ID> python3 sync_claude_memory.py
```

**查看/删除记忆条目：**
```bash
docker exec -it <容器ID> python3 inspect_memory.py
# 输入 core3 删除核心库第3条
# 输入 dyn2 删除动态库第2条
# 回车退出
```

---

## 环境变量

| 变量名 | 用途 |
|--------|------|
| `GEMINI_API_KEY` | Gemini embedding 模型 |
| `LLM_API_KEY` | DeepSeek 压缩用 |
| `PALACE_SECRET` | MCP 端点访问密码 |
| `GITHUB_WEBHOOK_SECRET` | Webhook 签名验证 |
| `GITHUB_TOKEN` | GitHub API 拉取文件（Personal Access Token，repo 权限） |
| `TOY_BRIDGE_URL` | Windows toy_bridge 地址（frpc 映射后的 VPS 内网地址） |
| `BROWSER_BRIDGE_URL` | Windows browser_bridge 地址（frpc 映射后的 VPS 内网地址） |
| `EMAIL_163_USER` | 163 邮箱地址 |
| `EMAIL_163_PASS` | 163 邮箱授权码（非登录密码） |

---

## 风险提示

**最高风险：embedding 模型名**
`memory_core.py` 硬编码了 `gemini-embedding-001`。Google 若弃用此模型会导致所有写入和检索失败（400 Bad Request）。定期检查 Google 模型弃用公告。

**次高风险：Docker 容器 ID**
每次 redeploy 容器 ID 都会变。手动进容器前必须先 `docker ps` 查最新 ID。

**第三：Windows 本地 bridge 掉线**
`toy_play` 和小红书 `browser_*` 依赖 Windows 本地进程在线，且 frpc 隧道需保持连接。设备功能失效时先检查 Windows 侧进程和 frpc 状态。

**第四：BLE 配对状态**
Satisfyer Curvy 2+ 需在 Windows 蓝牙设置里配对到 USB dongle（关闭内置网卡避免竞争），配对后不要重置。设备使用前需先用 Satisfyer Connect app 初始化一次。

**第五：Gemini API 免费 quota**
批量写入时每条都调 embedding，容易触发 429。sync 脚本已有 `time.sleep(1)` 缓解，quota 耗尽只能等每天太平洋时间午夜重置。

**第六：ChromaDB telemetry 报错**
启动时会打 `Failed to send telemetry event`，无害，忽略即可。

---

## Claude 连接配置

在 Claude.ai Settings → Integrations 添加：

```
https://erikssheep.uk/mcp/Jeoi2026/sse
```

新增或修改工具后需要在 Integrations 里**先断开再重新连接**（仅 reconnect 不够，会用缓存的旧工具列表）。

---

*最后更新：2026-04-30*
