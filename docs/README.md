# docs/ 文档地图

> 这个目录里既有纯文档，也有**网关运行时读取的文件**——后者的路径写死在代码里，改名或移动会直接弄坏线上服务。每个文件是什么、能不能动，都在下面。

---

## ⚙️ 运行时文件（不能改名、不能移动）

代码按 `docs/<文件名>` 的写死路径读取，VPS 上 `git pull` 后即生效。动之前必须同步改代码。

| 文件 | 是什么 | 谁在读 |
| --- | --- | --- |
| `nature_rules.json` | libido 触发时的性质候选规则表。条件本体在代码里，这里只放文案和阈值参数；文件顶部 `_readme` 字段有完整说明 | `desire_gateway.py` → `build_nature_hints`（每次现读） |
| `pinned_memories.json` | 固定注入词条表：📎 开启时消息命中触发词，词条整块注入。顶部 `_readme` 字段有完整说明 | `cc_ws_gateway.py`（`PINNED_MEM_FILE`），以及提示词里让 CC `cat` 它 |
| `diary_convention.md` | 日记约定：按【】节点写，Jeoi 在面板手动切分入动态库 | 网关提示词让 CC 写日记前 `cat` 它 |
| `coreading_convention.md` | 共读约定：批注、卡片等惯例，命中触发词时注入 | 注入链路（词条表引用） |

## 📐 设计文档（子系统总图）

| 文件 | 是什么 |
| --- | --- |
| `desire-system.md` | 渴望系统（内驱力引擎）架构总图：驱力、脉冲、衰减、注入链路 |
| `emotion-residue-system.md` | 情绪残留系统框架：驱力反复达标产生残留，残留反过来耦合驱力。是渴望系统的延伸（原名 `pulse.md`，2026-07-19 改名） |
| `记忆工程说明.md` | 2026-07-10 记忆分流工程总图：做了什么、代码在哪、怎么生效、哪些还没验证 |

## 🔧 运维与踩坑记录

| 文件 | 是什么 |
| --- | --- |
| `gpt-sovits-voice-training.md` | GPT-SoVITS 声音训练记录（音色、状态、参数） |
| `tts-pipeline-bug.md` | 语音通话句间卡顿的排查记录 |
| `stardew-tunnel-chain.md` | 星露谷 MCP 隧道链架构、frps/frpc 参数与踩过的坑 |
| `AK-G2_BLE_PROTOCOL.md` | AK-G2 (AfterKiss) BLE 协议逆向文档 |

## 📦 历史原因放在这里的代码/页面（先不动）

| 文件 | 是什么 |
| --- | --- |
| `ak_bridge.py` | AK-G2 BLE 桥接服务（端口 8768，frpc → VPS:7004）。部署侧可能按 `docs/ak_bridge.py` 路径启动，未验证前不移动 |
| `lot.html` | LOT 抽签页面 |

---

添加新文档时：设计文档和踩坑记录直接放进对应分类并更新本索引；如果是代码要读的运行时文件，在上面第一张表里登记清楚谁在读。
