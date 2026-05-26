"""
Claude Code CLI WebSocket Gateway
Translates between chat.html's WS protocol and Claude Code CLI's stream-json output.

Run: python cc_ws_gateway.py
Port: 8082
Caddy routes chat.erikssheep.uk/ws → localhost:8082/ws
"""

import asyncio
import json
import uuid
import os
import logging
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/opt/G-memory-mcp/logs/cc_gateway.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("cc-gw")

app = FastAPI(title="CC WebSocket Gateway")

PALACE_SECRET = os.getenv("PALACE_SECRET", "Jeoi2026")
CC_CWD = os.getenv("CC_CWD", "/opt/G-memory-mcp")
SGT = timezone(timedelta(hours=8))


class Session:
    def __init__(self, sid: str):
        self.id = sid
        self.name = f"Erik · {datetime.now(SGT).strftime('%m/%d %H:%M')}"
        self.cc_session_id: str | None = None
        self.created_at = datetime.now(SGT)
        self.preview = ""
        self.model = "claude-sonnet-4-6"
        self.effort = "medium"

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "preview": self.preview,
            "time": self.created_at.strftime("%H:%M"),
        }


sessions: dict[str, Session] = {}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    log.info("WS client connected")

    current_session: Session | None = None

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                log.warning(f"Bad JSON from client: {raw[:200]}")
                continue

            event = data.get("event", "")
            log.info(f"← {event} {json.dumps(data, ensure_ascii=False)[:200]}")

            if event == "session:list":
                await ws.send_json(
                    {
                        "event": "session:list",
                        "sessions": [s.to_dict() for s in sessions.values()],
                    }
                )

            elif event == "session:create":
                sid = uuid.uuid4().hex[:8]
                session = Session(sid)
                sessions[sid] = session
                current_session = session
                await ws.send_json(
                    {"event": "session:created", "sessionId": sid}
                )
                log.info(f"Session created: {sid}")

            elif event == "session:switch":
                sid = data.get("sessionId", "")
                if sid in sessions:
                    current_session = sessions[sid]
                    log.info(f"Switched to session: {sid}")

            elif event == "chat:send":
                message = data.get("message", "")
                if not message:
                    continue

                if not current_session:
                    sid = uuid.uuid4().hex[:8]
                    current_session = Session(sid)
                    sessions[sid] = current_session
                    await ws.send_json(
                        {"event": "session:created", "sessionId": sid}
                    )

                current_session.preview = message[:40]
                await run_claude(message, current_session, ws)

            elif event == "config:model":
                model = data.get("model", "")
                if current_session and model:
                    current_session.model = model
                    log.info(f"Model → {model}")

            elif event == "config:effort":
                effort = data.get("effort", "")
                if current_session and effort:
                    current_session.effort = effort
                    log.info(f"Effort → {effort}")

            elif event == "chat:respond":
                pass

            else:
                log.info(f"Unhandled event: {event}")

    except WebSocketDisconnect:
        log.info("WS client disconnected")
    except Exception as e:
        log.exception(f"WS error: {e}")


async def run_claude(message: str, session: Session, ws: WebSocket):
    """Spawn claude CLI in print mode and stream results back via WS."""

    cmd = [
        "claude",
        "--print",
        "--output-format", "stream-json",
        "--model", session.model,
        "--verbose",
    ]

    if session.cc_session_id:
        cmd.extend(["--resume", session.cc_session_id])

    cmd.extend(["--", message])

    log.info(f"Spawning: {' '.join(cmd[:6])}... (session={session.id}, cc={session.cc_session_id})")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=CC_CWD,
            env={**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
        )

        buffer = ""
        async for chunk in proc.stdout:
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                await handle_cli_line(line, session, ws)

        if buffer.strip():
            await handle_cli_line(buffer.strip(), session, ws)

        stderr_bytes = await proc.stderr.read()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        if stderr_text:
            log.warning(f"CLI stderr: {stderr_text[:500]}")

        await proc.wait()
        log.info(f"CLI exited with code {proc.returncode}")

        await ws.send_json({"event": "message:complete", "usage": {}})

    except Exception as e:
        log.exception(f"run_claude error: {e}")
        await ws.send_json({"event": "system:error", "message": str(e)})


async def handle_cli_line(line: str, session: Session, ws: WebSocket):
    """Parse one line of CLI stream-json output and relay to chat.html."""

    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        log.debug(f"Non-JSON line: {line[:200]}")
        await ws.send_json({"event": "stream:text", "text": line + "\n"})
        return

    log.debug(f"CLI event: {json.dumps(event, ensure_ascii=False)[:300]}")

    etype = event.get("type", "")

    # ── Format A: Anthropic API-style streaming events ──

    if etype == "message_start":
        msg = event.get("message", {})
        if msg.get("id"):
            session.cc_session_id = msg["id"]
        usage = msg.get("usage", {})
        if usage:
            await ws.send_json({"event": "system:usage", "usage": usage})

    elif etype == "content_block_start":
        block = event.get("content_block", {})
        btype = block.get("type", "")
        if btype == "tool_use":
            await ws.send_json(
                {
                    "event": "stream:block",
                    "block": {
                        "type": "tool_use",
                        "name": block.get("name", "tool"),
                        "input": block.get("input", {}),
                    },
                }
            )

    elif etype == "content_block_delta":
        delta = event.get("delta", {})
        dtype = delta.get("type", "")
        if dtype == "thinking_delta":
            text = delta.get("thinking", "")
            if text:
                await ws.send_json({"event": "stream:thinking", "text": text})
        elif dtype == "text_delta":
            text = delta.get("text", "")
            if text:
                await ws.send_json({"event": "stream:text", "text": text})
        elif dtype == "input_json_delta":
            pass

    elif etype == "content_block_stop":
        pass

    elif etype == "message_delta":
        usage = event.get("usage", {})
        if usage:
            await ws.send_json({"event": "system:usage", "usage": usage})

    elif etype == "message_stop":
        usage = event.get("message", {}).get("usage", {})
        await ws.send_json({"event": "message:complete", "usage": usage})

    # ── Format B: Claude Code CLI's own event format ──

    elif etype == "system":
        subtype = event.get("subtype", "")
        if subtype == "init":
            sid = event.get("session_id", "")
            if sid:
                session.cc_session_id = sid
                log.info(f"CC session ID: {sid}")

    elif etype == "assistant":
        message = event.get("message", {})
        content_blocks = message.get("content", [])
        if isinstance(content_blocks, list):
            for block in content_blocks:
                btype = block.get("type", "")
                if btype == "thinking":
                    text = block.get("thinking", "")
                    if text:
                        await ws.send_json({"event": "stream:thinking", "text": text})
                elif btype == "text":
                    text = block.get("text", "")
                    if text:
                        await ws.send_json({"event": "stream:text", "text": text})
                elif btype == "tool_use":
                    await ws.send_json(
                        {
                            "event": "stream:block",
                            "block": {
                                "type": "tool_use",
                                "name": block.get("name", "tool"),
                                "input": block.get("input", {}),
                            },
                        }
                    )
        usage = message.get("usage", {})
        if usage:
            await ws.send_json({"event": "system:usage", "usage": usage})

    elif etype == "result":
        usage = event.get("usage", {})
        cost = event.get("cost_usd", 0)
        session_id = event.get("session_id", "")
        if session_id:
            session.cc_session_id = session_id
        await ws.send_json(
            {"event": "message:complete", "usage": usage, "cost": cost}
        )

    elif etype == "tool":
        pass

    else:
        log.info(f"Unknown CLI event type: {etype}")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "sessions": len(sessions),
        "time": datetime.now(SGT).isoformat(),
    }


if __name__ == "__main__":
    import uvicorn

    os.makedirs("/opt/G-memory-mcp/logs", exist_ok=True)
    uvicorn.run(app, host="0.0.0.0", port=8082)
