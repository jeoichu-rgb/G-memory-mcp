"""Persistent Unix-socket bridge for the Devin ACP subprocess.

The chat gateway may be restarted for code updates while this service keeps
the Devin CLI process and its loaded sessions alive.  The socket deliberately
uses the same newline-delimited JSON-RPC shape as Devin ACP, so the gateway's
existing client, callbacks, Forge flow and usage accounting stay unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import grp
import json
import logging
import os
import signal
from pathlib import Path
from typing import Any

from devin_acp import DevinAcpClient, DevinAcpError


log = logging.getLogger("cc-gw.devin-bridge")


class DevinAcpBridge:
    """Own one Devin process and proxy ACP JSON-RPC over a Unix socket."""

    def __init__(
        self,
        *,
        socket_path: str,
        binary: str,
        model: str,
        cwd: str,
        run_as_user: str = "",
        home: str = "",
    ) -> None:
        self.socket_path = Path(socket_path)
        self.run_as_user = run_as_user
        self._clients: dict[asyncio.StreamWriter, asyncio.Lock] = {}
        self._request_tasks: set[asyncio.Task] = set()
        self._prompt_lock = asyncio.Lock()
        self._server: asyncio.AbstractServer | None = None
        self._client = DevinAcpClient(
            binary=binary,
            model=model,
            cwd=cwd,
            run_as_user=run_as_user,
            home=home,
            event_callback=self._broadcast_update,
        )

    async def start(self) -> None:
        await self._client.start()
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._accept, path=str(self.socket_path)
        )
        os.chmod(self.socket_path, 0o660)
        if self.run_as_user:
            with contextlib.suppress(KeyError, PermissionError):
                gid = grp.getgrnam(self.run_as_user).gr_gid
                os.chown(self.socket_path, -1, gid)
        log.info("Persistent Devin ACP bridge listening on %s", self.socket_path)

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for writer in list(self._clients):
            writer.close()
        for writer in list(self._clients):
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()
        self._clients.clear()
        if self._request_tasks:
            await asyncio.gather(*self._request_tasks, return_exceptions=True)
        await self._client.close()
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()

    async def _write(
        self, writer: asyncio.StreamWriter, message: dict[str, Any]
    ) -> bool:
        lock = self._clients.get(writer)
        if lock is None or writer.is_closing():
            return False
        wire = json.dumps(
            message, ensure_ascii=False, separators=(",", ":")
        ) + "\n"
        try:
            async with lock:
                writer.write(wire.encode("utf-8"))
                await writer.drain()
            return True
        except (ConnectionError, OSError):
            self._clients.pop(writer, None)
            writer.close()
            return False

    async def _broadcast_update(self, update: dict[str, Any]) -> None:
        message = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": update,
        }
        if self._clients:
            await asyncio.gather(
                *(self._write(writer, message) for writer in list(self._clients)),
                return_exceptions=True,
            )

    async def _proxy_request(
        self, writer: asyncio.StreamWriter, message: dict[str, Any]
    ) -> None:
        request_id = message.get("id")
        method = str(message.get("method") or "")
        params = (
            message.get("params")
            if isinstance(message.get("params"), dict)
            else {}
        )
        try:
            if method == "initialize":
                # The bridge already initialized the persistent ACP process.
                # Returning a local acknowledgement prevents a reconnecting
                # gateway from resetting it accidentally.
                result: dict[str, Any] = {
                    "protocolVersion": 1,
                    "agentInfo": {
                        "name": "g-memory-devin-acp-bridge",
                        "version": "1.0",
                    },
                }
            elif method == "session/prompt":
                async with self._prompt_lock:
                    result = await self._client.request(
                        method, params, timeout=900
                    )
            else:
                result = await self._client.request(method, params)
        except Exception as exc:
            await self._write(writer, {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32000,
                    "message": str(exc),
                },
            })
        else:
            await self._write(writer, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result,
            })

    async def _proxy_notification(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        params = (
            message.get("params")
            if isinstance(message.get("params"), dict)
            else {}
        )
        try:
            await self._client.notify(method, params)
        except DevinAcpError as exc:
            log.warning("ACP notification %s failed: %s", method, exc)

    def _track(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._request_tasks.add(task)
        task.add_done_callback(self._request_tasks.discard)

    async def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._clients[writer] = asyncio.Lock()
        log.info("Gateway connected to persistent Devin ACP bridge")
        try:
            while raw := await reader.readline():
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("Ignoring invalid bridge JSON: %r", raw[:300])
                    continue
                if not isinstance(message, dict) or not message.get("method"):
                    continue
                if "id" in message:
                    self._track(self._proxy_request(writer, message))
                else:
                    self._track(self._proxy_notification(message))
        finally:
            self._clients.pop(writer, None)
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()
            log.info(
                "Gateway disconnected; Devin ACP remains alive with sessions loaded"
            )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--socket",
        default=os.getenv(
            "DEVIN_ACP_SOCKET", "/run/g-memory-mcp/devin-acp.sock"
        ),
    )
    parser.add_argument(
        "--binary",
        default=os.getenv("DEVIN_BINARY", "/home/erik/.local/bin/devin"),
    )
    parser.add_argument(
        "--model",
        default=os.getenv("DEVIN_MODEL_ID", "claude-opus-4-6-thinking"),
    )
    parser.add_argument("--cwd", default=os.getenv("CC_CWD", "/opt/G-memory-mcp"))
    parser.add_argument("--user", default=os.getenv("DEVIN_USER", "erik"))
    parser.add_argument(
        "--home",
        default=os.getenv("DEVIN_HOME", "/home/erik"),
    )
    return parser.parse_args()


async def _main() -> None:
    args = _arguments()
    bridge = DevinAcpBridge(
        socket_path=args.socket,
        binary=args.binary,
        model=args.model,
        cwd=args.cwd,
        run_as_user=args.user,
        home=args.home,
    )
    await bridge.start()
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stopping.set)
    await stopping.wait()
    await bridge.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_main())
