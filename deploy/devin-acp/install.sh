#!/bin/bash
set -euo pipefail

PROJECT_DIR=/opt/G-memory-mcp
BRIDGE_UNIT=/etc/systemd/system/devin-acp-bridge.service
GATEWAY_DROPIN_DIR=/etc/systemd/system/cc-gateway.service.d

cd "$PROJECT_DIR"
install -m 0644 deploy/devin-acp/devin-acp-bridge.service "$BRIDGE_UNIT"
install -d -m 0755 "$GATEWAY_DROPIN_DIR"
install -m 0644 deploy/devin-acp/cc-gateway.conf \
  "$GATEWAY_DROPIN_DIR/devin-acp.conf"

systemctl daemon-reload
systemctl enable --now devin-acp-bridge.service

for _attempt in $(seq 1 40); do
  if [ -S /run/g-memory-mcp/devin-acp.sock ]; then
    break
  fi
  sleep 0.25
done

if [ ! -S /run/g-memory-mcp/devin-acp.sock ]; then
  systemctl status devin-acp-bridge.service --no-pager || true
  exit 1
fi

# This is the one migration restart.  From now on restarting only this unit
# disconnects/reconnects the socket and does not terminate or resume Devin.
systemctl restart cc-gateway.service
systemctl is-active --quiet devin-acp-bridge.service
systemctl is-active --quiet cc-gateway.service

echo "Persistent Devin ACP bridge installed. Gateway restarts no longer reload Devin sessions."
