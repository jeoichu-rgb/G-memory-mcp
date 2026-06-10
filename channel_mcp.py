"""
channel_mcp.py - CC CLI 的 channel plugin（stdio MCP server）
================================================================
CC CLI 把本文件当子进程启动（stdio transport），用于：
  1. 接收 gateway 发来的用户消息（via internal WS）
  2. 通过 notifications/claude/channel 推送给 CC CLI
  3. 截获 CC CLI 调用的 reply 工具 -> 转发回 gateway
  4. 截获 CC CLI 调用的 reply_chunk 工具 -> 流式转发

.claude/settings.json 配置（VPS上）：
  {
    "mcpServers": {
      "erik_channel": {
        "command": "python3",
        "args": ["/opt/G-memory-mcp/channel_mcp.py"]
      }
    }
  }

CC CLI 启动时需要加：
  --dangerously-load-development-channels server:erik_channel

环境变量：
  GATEWAY_WS_URL  - gateway 内部 WS 地址（默认 ws://127.0.0.1:3000/internal/channel）
  CHANNEL_SOURCE  - channel source name（默认 erik）
"""

import asyncio
import json
import os
import sys
import logging
from typing import Any

import mcp.types as types
from mcp.types import JSONRPCNotification, JSONRPCMessage
from mcp.shared.message import SessionMessage
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

# -- Config --
GATEWAY_WS_URL = os.getenv("GATEWAY_WS_URL", "ws://127.0.0.1:3000/internal/channel")
CHANNEL_SOURCE = os.getenv("CHANNEL_SOURCE", "erik")

# -- Logging (stderr only - stdout is MCP stdio transport) --
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [channel] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("channel")

# -- MCP Server --
server = Server(name=CHANNEL_SOURCE, version="1.0.0")

# Global write stream ref - set when session starts, used to push notifications
_write_stream = None


# ============================================================
#  Tool definitions
# ============================================================

REPLY_TOOL = types.Tool(
    name="reply",
    description=(
        "Send a message to Jeoi via the web UI. "
        "Mirror chat_id from the incoming channel meta. "
        "Keep text SHORT (<500 chars); overly long text may be silently dropped - "
        "split into multiple reply calls if needed."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "maxLength": 2000},
            "chat_id": {"type": "string", "enum": ["cc", "system"]},
            "reply_to": {"type": "string", "description": "message_id to quote-reply"},
        },
        "required": ["text"],
    },
)

REPLY_CHUNK_TOOL = types.Tool(
    name="reply_chunk",
    description=(
        "Stream a reply progressively. Same chat_id, multiple calls. "
        "Set done=true on the final chunk."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "chat_id": {"type": "string"},
            "done": {"type": "boolean"},
        },
        "required": ["text", "chat_id", "done"],
    },
)


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [REPLY_TOOL, REPLY_CHUNK_TOOL]


# -- Gateway WS connection (global, reconnecting) --
_gw_ws = None  # websockets connection object


async def _gw_send(msg: dict):
    """Send a message to the gateway via internal WS."""
    global _gw_ws
    if _gw_ws is None:
        log.warning("gateway WS not connected, dropping message")
        return
    try:
        await _gw_ws.send(json.dumps(msg, ensure_ascii=False))
    except Exception as e:
        log.warning(f"gateway send failed: {e}")
        _gw_ws = None


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict[str, Any]
) -> list[types.TextContent]:
    """Handle reply / reply_chunk tool calls from CC CLI."""
    if name == "reply":
        text = arguments.get("text", "")
        chat_id = arguments.get("chat_id", "cc")
        reply_to = arguments.get("reply_to")
        log.info(f"reply [{chat_id}]: {text[:80]}")
        await _gw_send({
            "type": "reply",
            "text": text,
            "chat_id": chat_id,
            "reply_to": reply_to,
        })
        return [types.TextContent(type="text", text="sent")]

    elif name == "reply_chunk":
        text = arguments.get("text", "")
        chat_id = arguments.get("chat_id", "cc")
        done = arguments.get("done", False)
        await _gw_send({
            "type": "reply_chunk",
            "text": text,
            "chat_id": chat_id,
            "done": done,
        })
        return [types.TextContent(type="text", text="chunk_sent")]

    return [types.TextContent(type="text", text=f"unknown tool: {name}")]


# ============================================================
#  Channel notification push
# ============================================================

async def push_channel_message(content: str, meta: dict):
    """Push a notifications/claude/channel to CC CLI via stdio."""
    if _write_stream is None:
        log.warning("write_stream not ready, cannot push channel notification")
        return
    notification = JSONRPCNotification(
        jsonrpc="2.0",
        method="notifications/claude/channel",
        params={"content": content, "meta": meta},
    )
    message = SessionMessage(message=JSONRPCMessage(notification))
    try:
        await _write_stream.send(message)
        log.info(f"pushed channel notification: {content[:80]}")
    except Exception as e:
        log.error(f"failed to push channel notification: {e}")


# ============================================================
#  Gateway WS listener (background task)
# ============================================================

async def gateway_listener():
    """Connect to gateway internal WS, listen for user messages.
    Auto-reconnects on failure."""
    global _gw_ws

    try:
        import websockets
    except ImportError:
        log.error("websockets package not installed! pip install websockets")
        return

    while True:
        try:
            log.info(f"connecting to gateway: {GATEWAY_WS_URL}")
            async with websockets.connect(GATEWAY_WS_URL) as ws:
                _gw_ws = ws
                log.info("gateway connected")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        log.warning(f"bad JSON from gateway: {raw[:200]}")
                        continue

                    msg_type = msg.get("type", "")

                    if msg_type == "user_message":
                        # User sent a message -> push to CC CLI
                        content = msg.get("text", "")
                        meta = {
                            "chat_id": msg.get("chat_id", "cc"),
                            "sender": msg.get("from", "Jeoi"),
                        }
                        if msg.get("message_id"):
                            meta["message_id"] = msg["message_id"]
                        await push_channel_message(content, meta)

                    elif msg_type == "ping":
                        await _gw_send({"type": "pong"})

                    else:
                        log.debug(f"unknown gateway msg type: {msg_type}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            _gw_ws = None
            log.warning(f"gateway WS error: {e}, reconnecting in 3s...")
            await asyncio.sleep(3)


# ============================================================
#  Main
# ============================================================

async def main():
    global _write_stream

    async with stdio_server() as (read_stream, write_stream):
        _write_stream = write_stream

        init_options = server.create_initialization_options(
            experimental_capabilities={"claude/channel": {}},
        )

        # Start gateway listener in background
        listener_task = asyncio.create_task(gateway_listener())

        try:
            await server.run(read_stream, write_stream, init_options)
        finally:
            listener_task.cancel()
            try:
                await listener_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
