# 记忆宫殿 · Erik's Memory Palace

> 一套运行在私人服务器上的 AI 记忆系统，让 Claude 在不同对话窗口之间能记住你说过的事情。

---

## 是什么

Claude 每次开新窗口就失忆。这个系统通过 MCP（Model Context Protocol）给 Claude 挂载一套持久化记忆库，让他能跨窗口检索、存储、压缩记忆，并通过日记系统留下痕迹。

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

---

## 记忆库结构

ChromaDB 存储在 `/app/chroma_db/`（持久化卷挂载）。

### 两个记忆库

| 库名 | 类型 | 特性 |
|------|------|------|
| `claude_core_palace` | 核心记忆 | 永久，不衰减，来自 Obsidian 同步 |
| `claude_dynamic_palace` | 动态记忆 | 有遗忘曲线，来自对话压缩 |

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

Claude.ai 通过 SSE 端点调用以下工具：

| 工具 | 什么时候用 |
|------|-----------|
| `get_context` | 对话开始时冷启动，读最近两次压缩总结 |
| `search_memory` | 关键词向量检索，同时搜核心库和动态库 |
| `store_core_memory` | 永久存入核心库，不会遗忘 |
| `store_dynamic_memory` | 存入动态库，时间久了会衰减 |
| `log_turn` | 每轮对话追加到缓冲文件 |
| `compress_memory` | 手动触发压缩，DeepSeek 将缓冲区存入动态库 |
| `list_room` | 浏览某个房间的全部记忆标题和摘要 |
| `write_diary` | 写日记 MD 文件到 VPS |
| `read_diary` | 读取日记 |
| `append_diary` | 给某天日记追加内容 |

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

---

## 风险提示

**最高风险：embedding 模型名**
`memory_core.py` 硬编码了 `gemini-embedding-001`。Google 若弃用此模型会导致所有写入和检索失败（400 Bad Request）。定期检查 Google 模型弃用公告。

**次高风险：Docker 容器 ID**
每次 redeploy 容器 ID 都会变。手动进容器前必须先 `docker ps` 查最新 ID。

**第三：Gemini API 免费 quota**
批量写入时每条都调 embedding，容易触发 429。sync 脚本已有 `time.sleep(1)` 缓解，quota 耗尽只能等每天太平洋时间午夜重置。

**第四：ChromaDB telemetry 报错**
启动时会打 `Failed to send telemetry event`，无害，忽略即可。

---

## Claude 连接配置

在 Claude.ai Settings → Integrations 添加：

```
https://你的域名/mcp/<PALACE_SECRET>/sse
```

---

*最后更新：2026-04-15*
