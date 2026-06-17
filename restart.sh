#!/bin/bash
cd /opt/G-memory-mcp || exit 1
fuser -k 3000/tcp 2>/dev/null
sudo -u erik tmux kill-server 2>/dev/null
sleep 1
git checkout . 2>/dev/null
git pull

# 加载环境变量（Coolify 只管 Docker 容器，宿主机进程需要自己加载）
if [ -f /opt/G-memory-mcp/.env ]; then
  set -a
  source /opt/G-memory-mcp/.env
  set +a
fi

nohup python3 cc_ws_gateway.py >> logs/cc_gateway.log 2>&1 &
echo "gateway started (pid $!)"
