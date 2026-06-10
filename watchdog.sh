#!/usr/bin/env bash
# Watchdog: curl /health every minute (via cron), restart gateway if down.
# Install: crontab -e → * * * * * /opt/G-memory-mcp/watchdog.sh
PORT="${CC_GW_PORT:-8081}"
LOG="/opt/G-memory-mcp/logs/watchdog.log"

if ! curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    echo "$(date '+%F %T') FAIL: gateway not responding, restarting" >> "$LOG"
    systemctl restart cc-gateway 2>>"$LOG" || echo "$(date '+%F %T') systemctl restart failed" >> "$LOG"
else
    # Check if PersistentCLI is alive (parse JSON health response)
    CLI_RUNNING=$(curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/health" 2>/dev/null \
        | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('cli',{}).get('running',False))" 2>/dev/null)
    if [ "$CLI_RUNNING" = "False" ]; then
        echo "$(date '+%F %T') WARN: CLI not running, restarting gateway" >> "$LOG"
        systemctl restart cc-gateway 2>>"$LOG"
    fi
fi
