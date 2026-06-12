#!/bin/bash
cd /opt/G-memory-mcp || exit 1
fuser -k 3000/tcp 2>/dev/null
sudo -u erik tmux kill-server 2>/dev/null
sleep 1
git checkout .claude/settings.json 2>/dev/null
git pull
nohup python3 cc_ws_gateway.py >> logs/cc_gateway.log 2>&1 &
echo "gateway started (pid $!)"
