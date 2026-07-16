---
name: stardew-tunnel-chain
description: 星露谷 MCP 公链架构、frps/frpc 连接参数（bindPort 24816 无 token）与已踩过的坑（session-bound 进程、frps 90s proxy 名残留、frp 目录被误删过一次）
metadata: 
  node_type: memory
  type: project
  originSessionId: 8c0a8833-4110-4215-8155-0962fcf77b16
---

星露谷 MCP 公链：erikssheep.uk (VPS nginx) → frps (192.3.61.205:24816) → frpc (D:\Eric\frp, 走 Clash :7897 或直连) → 本地 node MCP server :7845 → bridge_data.json ↔ 游戏 mod。健康检查 https://erikssheep.uk/stardew/Jeoi2026/health。

**坑一**：MCP server 不能用 CC 后台任务启动——session 结束进程被回收，公网直接 502。要么 Jeoi 双击 D:\Eric\StardewValley-MCP\start-stardew.bat（窗口常驻），要么用 Start-Process detached。

**坑二**：被强杀的 frpc 在 frps 端的 proxy 名要 ~90 秒（心跳超时）才释放（走 Clash 时 frps 看不到 TCP 断开）。过早重启 frpc 会 "proxy already exists"，被拒的 proxy frpc 不重试，导致部分桥（如 bunnybridge）静默丢失。重启 frpc 必须等 90s+。start-stardew.ps1 和 frpc-watchdog.ps1 已于 2026-07-11 修复此竞态。

**排查顺序**：先 http://localhost:7845/health（server 活没活），再看 bridge_data.json 的 syncedAt 新不新（游戏失焦会暂停、退标题画面停止同步，action 文件堆在 actions 目录不消费），最后才怀疑 frpc/frps。

**frp 连接参数**（2026-07-16 因 D:\Eric\frp 整目录被绕回收站误删、全套重建后补记，防止再丢）：frps 跑在 VPS `/root/frp_0.61.1_linux_amd64/frps`，配置只有一行 `bindPort = 24816`，**没有 auth token**——frpc.toml 绝不能带 auth 段，否则登录被拒。隧道映射：stardew 7845→7005、toybridge 8765→7001、ak 8768→7004；bunny(→7003) 和 browser(→7002) 的 bridge 本体已丢，browser 正式退役，bunny 待重建。

**watchdog**：计划任务 `\frpc-watchdog` 每 5 分钟 wscript 跑 `D:\Eric\frp\frpc-watchdog.vbs` → ps1。2026-07-16 重建时修了一个原版 bug：本地 :7845 没在监听时公网 health 必 fail，旧版会每 5 分钟无谓重启 frpc（连累 ak/toy 隧道断 90s）；新版此时只在 frpc 进程死了才拉起。若 Windows 弹"无法找到脚本文件 frpc-watchdog.vbs"，说明 frp 目录又没了而任务还在。
