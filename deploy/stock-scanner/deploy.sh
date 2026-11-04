#!/bin/bash
# Stock Scanner MCP 部署脚本 — 在 VPS 上跑
# 跑完之后去 Claude 桌面端 Settings → Connectors → + Add
# 加一条 Web Custom，SSE URL: https://erikssheep.uk/stock/Jeoi2026/sse

set -e

echo "=== 1. 安装依赖 ==="
mkdir -p /opt/stock-scanner
cd /opt/stock-scanner
npm init -y
npm install supergateway stock-scanner-mcp

echo "=== 2. 安装 systemd service ==="
cp /root/G-memory-mcp/deploy/stock-scanner/stock-scanner.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable stock-scanner
systemctl start stock-scanner

echo "=== 3. 等两秒检查进程 ==="
sleep 2
systemctl status stock-scanner --no-pager

echo "=== 4. 测试本地端口 ==="
curl -s http://127.0.0.1:8090/health && echo " ← health OK" || echo " ← health FAILED"

echo ""
echo "=== 接下来手动做 ==="
echo "1) 把 nginx.conf 里的 location 块加到 erikssheep.uk 的 server block 里"
echo "2) nginx -t && systemctl reload nginx"
echo "3) curl https://erikssheep.uk/stock/Jeoi2026/health  验证公网通路"
echo "4) Claude 桌面端 → Settings → Connectors → + Add → Web Custom"
echo "   URL: https://erikssheep.uk/stock/Jeoi2026/sse"
