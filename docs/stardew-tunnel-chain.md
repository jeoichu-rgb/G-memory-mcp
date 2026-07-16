---
name: stardew-tunnel-chain
description: 星露谷 MCP 公链架构、frps/frpc 连接参数（bindPort 24816 + auth token）与已踩过的坑（session-bound 进程、frps 90s proxy 名残留、frp 目录被误删过一次）
metadata: 
  node_type: memory
  type: project
  originSessionId: 8c0a8833-4110-4215-8155-0962fcf77b16
---

星露谷 MCP 公链：erikssheep.uk (VPS nginx) → frps (192.3.61.205:24816) → frpc (D:\Eric\frp, 走 Clash :7897 或直连) → 本地 node MCP server :7845 → bridge_data.json ↔ 游戏 mod。健康检查 https://erikssheep.uk/stardew/Jeoi2026/health。

**坑一**：MCP server 不能用 CC 后台任务启动——session 结束进程被回收，公网直接 502。要么 Jeoi 双击 D:\Eric\StardewValley-MCP\start-stardew.bat（窗口常驻），要么用 Start-Process detached。

**坑二**：被强杀的 frpc 在 frps 端的 proxy 名要 ~90 秒（心跳超时）才释放（走 Clash 时 frps 看不到 TCP 断开）。过早重启 frpc 会 "proxy already exists"，被拒的 proxy frpc 不重试，导致部分桥（如 bunnybridge）静默丢失。重启 frpc 必须等 90s+。start-stardew.ps1 和 frpc-watchdog.ps1 已于 2026-07-11 修复此竞态。

**排查顺序**：先 http://localhost:7845/health（server 活没活），再看 bridge_data.json 的 syncedAt 新不新（游戏失焦会暂停、退标题画面停止同步，action 文件堆在 actions 目录不消费），最后才怀疑 frpc/frps。

**frp 连接参数**（2026-07-16 因 D:\Eric\frp 整目录被绕回收站误删、全套重建后补记，防止再丢）：frps 跑在 VPS `/root/frp_0.61.1_linux_amd64/frps`，`bindPort = 24816`。2026-07-16 起两边启用 `auth.method = "token"`——**token 值不进 git**，只存在于 `D:\Eric\frp\frpc.toml` 和 VPS `/root/frp_0.61.1_linux_amd64/frps.toml` 两处，且必须一致（不一致 frpc 登录秒退）。改 token 或查 token 去这两个文件看。隧道映射：stardew 7845→7005、toybridge 8765→7001、ak 8768→7004；bunny(→7003) 和 browser(→7002) 的 bridge 本体已丢，browser 正式退役，bunny 待重建。

**watchdog**：计划任务 `\frpc-watchdog` 每 5 分钟 wscript 跑 `D:\Eric\frp\frpc-watchdog.vbs` → ps1。2026-07-16 重建时修了一个原版 bug：本地 :7845 没在监听时公网 health 必 fail，旧版会每 5 分钟无谓重启 frpc（连累 ak/toy 隧道断 90s）；新版此时只在 frpc 进程死了才拉起。若 Windows 弹"无法找到脚本文件 frpc-watchdog.vbs"，说明 frp 目录又没了而任务还在。

---

## 全灭复活手册（2026-07-16 实战验证,新 session 零上下文可直接照做）

适用场景：`D:\Eric\frp` 又整个消失 / 换新电脑 / 从零重建。全程约 10 分钟。
源代码都在 git（repo 本体 + jeoichu-rgb/StardewValley-MCP），会丢的只有下面这些"git 外的可再生文件"，本手册全部覆盖。

### 第 1 步：下载 frp

```powershell
# Clash 开着就借道,没开就去掉 --proxy 参数
curl.exe -L --proxy http://127.0.0.1:7897 -o D:\Eric\frp\frp.zip https://github.com/fatedier/frp/releases/download/v0.61.1/frp_0.61.1_windows_amd64.zip
Expand-Archive D:\Eric\frp\frp.zip D:\Eric\frp\ -Force; Remove-Item D:\Eric\frp\frp.zip
```

得到 `D:\Eric\frp\frp_0.61.1_windows_amd64\frpc.exe`（这个路径被 start-stardew.ps1 和 watchdog 写死,别挪）。

### 第 2 步：写 frpc.toml

存为 `D:\Eric\frp\frpc.toml`。token 值去 VPS 看：`cat /root/frp_0.61.1_linux_amd64/frps.toml`（Coolify → Terminal 就是 VPS 本机,不用再 ssh）。

```toml
serverAddr = "192.3.61.205"
serverPort = 24816
auth.method = "token"
auth.token = "<与 VPS frps.toml 的 auth.token 一致,不进 git>"

[[proxies]]
name = "stardew"
type = "tcp"
localIP = "127.0.0.1"
localPort = 7845
remotePort = 7005

[[proxies]]
name = "toybridge"
type = "tcp"
localIP = "127.0.0.1"
localPort = 8765
remotePort = 7001

[[proxies]]
name = "ak"
type = "tcp"
localIP = "127.0.0.1"
localPort = 8768
remotePort = 7004
```

（bunny→7003、browser→7002 的 bridge 本体已丢,browser 退役,bunny 重建后再加。）

### 第 3 步：watchdog 三件套

`D:\Eric\frp\frpc-watchdog.vbs`（无窗口包装,就一行）：

```vbs
CreateObject("Wscript.Shell").Run "powershell -NoProfile -ExecutionPolicy Bypass -File ""D:\Eric\frp\frpc-watchdog.ps1""", 0, False
```

`D:\Eric\frp\frpc-watchdog.ps1`（全文）：

```powershell
$FRP_DIR = "D:\Eric\frp"
$FRPC    = Join-Path $FRP_DIR "frp_0.61.1_windows_amd64\frpc.exe"
$TOML    = Join-Path $FRP_DIR "frpc.toml"
$LOG     = Join-Path $FRP_DIR "watchdog.log"
$HEALTH  = "https://erikssheep.uk/stardew/Jeoi2026/health"

# 本地 MCP(:7845)没开时,公网 health 必然 fail——那不是隧道的错。
# 此时只保证 frpc 进程活着,不做公网判定,免得五分钟一次的重启把 ak/toy 隧道也折腾断。
$mcpUp = [bool](Get-NetTCPConnection -LocalPort 7845 -State Listen -ErrorAction SilentlyContinue)
$frpcAlive = [bool](Get-Process frpc -ErrorAction SilentlyContinue)
if (-not $mcpUp) {
    if ($frpcAlive) { exit 0 }
    $reason = "frpc not running (local mcp down)"
} else {
    try { $ok = [bool](Invoke-RestMethod $HEALTH -TimeoutSec 12).ok } catch { $ok = $false }
    if ($ok) { exit 0 }
    $reason = "public health failed"
}

Get-Process frpc -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

$via = "direct"
if (Get-NetTCPConnection -LocalPort 7897 -State Listen -ErrorAction SilentlyContinue) {
    $env:HTTP_PROXY  = "http://127.0.0.1:7897"
    $env:HTTPS_PROXY = "http://127.0.0.1:7897"
    $via = "Clash"
}
Start-Process -FilePath $FRPC -ArgumentList "-c", $TOML -WorkingDirectory $FRP_DIR -WindowStyle Hidden
"{0:yyyy-MM-dd HH:mm:ss}  {1} - restarted frpc via {2}" -f (Get-Date), $reason, $via | Add-Content -Path $LOG -Encoding utf8
```

计划任务（若 `schtasks /query /tn frpc-watchdog` 说不存在才需要建；存在但 Disabled 则 `/change /enable`）：

```powershell
schtasks /create /tn "frpc-watchdog" /tr "wscript.exe \"D:\Eric\frp\frpc-watchdog.vbs\"" /sc minute /mo 5 /f
```

### 第 4 步：编译星露谷 MCP server

build/ 是 git 外产物,丢了就重编。**坑：tsc 在 devDependencies 里,`npm install` 若装出残包会报 `'tsc' is not recognized`,必须带 `--include=dev`**：

```powershell
cd D:\Eric\StardewValley-MCP\mcp-server
npm install --include=dev
npm run build      # 产物 build\index.js,start-stardew.ps1 按这个路径起 node
```

### 第 5 步：点火验证

双击 `D:\Eric\StardewValley-MCP\start-stardew.bat`,窗口里依次看到：
`login to server success` → `proxy added: [stardew toybridge ak]`（三条 start proxy success）→ `[OK] local health: ok=True` → **`[OK] public chain up`** = 全通。
然后 claude.ai → Settings → Connectors → stardew（`https://erikssheep.uk/stardew/Jeoi2026/sse`）点 Connect。

窗口里若滚 `[toybridge] connect to local service refused` 红字：不是故障,是 VPS 那头在探玩具隧道而本地 toy_bridge 没开,无害。

### 日常速查

| 想干什么 | 开什么 |
|---|---|
| 星露谷 | 双击 start-stardew.bat（frpc 它自己接管） |
| 玩具 | `python D:\Eric\toy_bridge.py`（Curvy）或 `python D:\Eric\ak_bridge.py`（AK）,顺序随意 |
| 什么都不开 | watchdog 自动保 frpc 常活,开机 5 分钟内就位 |
