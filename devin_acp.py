"""Async Devin ACP client used by the chat gateway and bridge.

The Devin CLI exposes an Agent Client Protocol server over newline-delimited
JSON-RPC on stdin/stdout.  In production the subprocess can be owned by the
standalone :mod:`devin_acp_bridge` service; the gateway then speaks the same
wire protocol over a Unix socket.  Keeping both transports behind one client
lets the bridge survive gateway code reloads without reloading Devin sessions.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


log = logging.getLogger("cc-gw.devin")

EventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


def _trace_usage(source: str, session_id: str, payload: dict[str, Any]) -> None:
    """Log ACP field paths and numeric counters without logging message bodies."""
    counters: dict[str, int | float | str | None] = {}

    def collect(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                collect(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                collect(child, f"{path}[{index}]")
        else:
            name = path.casefold()
            leaf = name.rsplit(".", 1)[-1]
            if "token" in name or "cache" in name or leaf in {"used", "size", "usage"}:
                if value is None or type(value) in (int, float):
                    counters[path] = value
                elif (
                    isinstance(value, str)
                    and len(value) <= 20
                    and value.isascii()
                    and value.isdecimal()
                ):
                    counters[path] = value
                else:
                    counters[path] = f"<{type(value).__name__}>"

    collect(payload, "$")
    log.info(
        "Devin ACP usage trace: source=%s session=%s keys=%s counters=%s",
        source, session_id[:8], sorted(payload),
        json.dumps(counters, sort_keys=True),
    )


def normalize_mcp_servers(
    raw_servers: dict[str, Any],
) -> list[dict[str, Any]]:
    """Translate Claude-style MCP config to strict ACP v1 wire objects.

    ACP v1 requires headers for HTTP/SSE, args and env for stdio,
    and uses no type discriminator for the stdio variant.
    """
    result: list[dict[str, Any]] = []
    for name, config in raw_servers.items():
        if not isinstance(config, dict):
            continue
        url = str(config.get("url") or "")
        if url:
            server_type = str(config.get("type") or "")
            if server_type not in ("http", "sse"):
                server_type = (
                    "sse" if url.rstrip("/").endswith("/sse") else "http"
                )
            headers: list[dict[str, str]] = []
            raw_headers = config.get("headers")
            if isinstance(raw_headers, dict):
                headers = [
                    {"name": str(key), "value": str(value)}
                    for key, value in raw_headers.items()
                ]
            elif isinstance(raw_headers, list):
                headers = [
                    {
                        "name": str(item["name"]),
                        "value": str(item["value"]),
                    }
                    for item in raw_headers
                    if isinstance(item, dict)
                    and "name" in item
                    and "value" in item
                ]
            result.append({
                "name": str(name),
                "type": server_type,
                "url": url,
                "headers": headers,
            })
            continue

        command = config.get("command")
        if not command:
            continue
        raw_env = config.get("env")
        env: list[dict[str, str]] = []
        if isinstance(raw_env, dict):
            env = [
                {"name": str(key), "value": str(value)}
                for key, value in raw_env.items()
            ]
        elif isinstance(raw_env, list):
            env = [
                {
                    "name": str(item["name"]),
                    "value": str(item["value"]),
                }
                for item in raw_env
                if isinstance(item, dict)
                and "name" in item
                and "value" in item
            ]
        result.append({
            "name": str(name),
            "command": str(command),
            "args": [str(value) for value in config.get("args", [])],
            "env": env,
        })
    return result


class DevinAcpError(RuntimeError):
    """ACP process or JSON-RPC error."""


class DevinModelUnavailable(DevinAcpError):
    """The account did not actually activate the requested model."""


@dataclass(slots=True)
class DevinPromptResult:
    stop_reason: str
    usage: dict[str, Any]
    raw: dict[str, Any]


def _config_options(result: dict[str, Any]) -> list[dict[str, Any]]:
    options = result.get("configOptions")
    if options is None:
        options = result.get("config_options")
    return options if isinstance(options, list) else []


def _option_id(option: dict[str, Any]) -> str:
    return str(option.get("id") or option.get("configId") or "")


def _option_current(option: dict[str, Any]) -> str:
    value = option.get("currentValue", option.get("current", option.get("value", "")))
    if isinstance(value, dict):
        value = value.get("value") or value.get("id") or value.get("model_uid") or ""
    return str(value)


def find_config_option(result: dict[str, Any], category: str) -> dict[str, Any] | None:
    """Return an ACP config option by id/category, tolerating schema aliases."""
    wanted = category.casefold().replace("-", "_")
    for option in _config_options(result):
        labels = {
            str(option.get("id") or ""),
            str(option.get("category") or ""),
            str(option.get("type") or ""),
            str(option.get("label") or ""),
        }
        normalized = {label.casefold().replace("-", "_").replace(" ", "_")
                      for label in labels if label}
        if wanted in normalized:
            return option
    return None


def normalize_session_update(params: dict[str, Any]) -> dict[str, Any]:
    """Flatten both ACP's standard update envelope and Devin probe logs."""
    update = params.get("update")
    if isinstance(update, dict):
        flat = dict(update)
        if "sessionId" not in flat and params.get("sessionId"):
            flat["sessionId"] = params["sessionId"]
        return flat
    return dict(params)


def cache_read_tokens(usage: dict[str, Any]) -> int | None:
    """Preserve the difference between an explicit zero and missing telemetry."""
    meta = usage.get("_meta")
    if isinstance(meta, dict):
        for key in ("cognition.ai/cachedInputTokens",
                    "cognition.ai/cacheReadInputTokens",
                    "cognition.ai/cachedReadTokens"):
            if meta.get(key) is not None:
                return int(meta[key])
    for key in ("cachedInputTokens", "cacheReadInputTokens",
                "cache_read_input_tokens", "cached_input_tokens", "cachedReadTokens"):
        if usage.get(key) is not None:
            return int(usage[key])
    return None


def extract_usage(update: dict[str, Any]) -> dict[str, Any]:
    """Normalize Devin's usage_update, including its token metadata."""
    meta = update.get("_meta") if isinstance(update.get("_meta"), dict) else {}
    cost = update.get("cost") or update.get("cumulativeCost") or meta.get("cognition.ai/cost")
    amount = None
    currency = None
    if isinstance(cost, dict):
        amount = cost.get("amount", cost.get("value"))
        currency = cost.get("currency")
    elif isinstance(cost, (int, float)):
        amount = cost
    return {
        "context_used": int(update.get("used") or 0),
        "context_size": int(update.get("size") or 0),
        "input_tokens": int(
            meta.get("cognition.ai/inputTokens")
            or update.get("inputTokens")
            or update.get("input_tokens")
            or 0
        ),
        "output_tokens": int(
            meta.get("cognition.ai/outputTokens")
            or update.get("outputTokens")
            or update.get("output_tokens")
            or 0
        ),
        "cached_input_tokens": cache_read_tokens(update),
        "cost": float(amount) if amount is not None else None,
        "currency": str(currency or "USD"),
    }


class DevinAcpClient:
    """One persistent devin acp process serving multiple Devin sessions."""

    def __init__(
        self,
        *,
        binary: str,
        model: str,
        cwd: str,
        run_as_user: str = "",
        home: str = "",
        socket_path: str = "",
        event_callback: EventCallback | None = None,
    ):
        self.binary = binary
        self.model = model
        self.cwd = cwd
        self.run_as_user = run_as_user
        self.home = home
        # The gateway predates the bridge and constructs this client without
        # transport arguments.  Reading the opt-in environment variable here
        # keeps that call site untouched while the systemd drop-in moves it to
        # the persistent bridge transport.
        self.socket_path = socket_path or os.getenv("DEVIN_ACP_SOCKET", "")
        self.trace_usage = os.getenv("DEVIN_ACP_USAGE_TRACE") == "1"
        self._event_callback = event_callback
        self._proc: asyncio.subprocess.Process | None = None
        self._socket_reader: asyncio.StreamReader | None = None
        self._socket_writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._callbacks: dict[str, EventCallback] = {}
        self._start_lock = asyncio.Lock()
        # Devin processes one foreground prompt at a time. Keeping this
        # explicit also makes callback routing deterministic.
        self._prompt_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        if self.socket_path:
            return (
                self._socket_writer is not None
                and not self._socket_writer.is_closing()
            )
        return self._proc is not None and self._proc.returncode is None

    def _command(self) -> list[str]:
        base = [self.binary, "acp", "--model", self.model]
        is_root = getattr(os, "geteuid", lambda: -1)() == 0
        if self.run_as_user and is_root and self.run_as_user != "root":
            return ["sudo", "-H", "-u", self.run_as_user, *base]
        return base

    async def start(self) -> None:
        async with self._start_lock:
            if self.running:
                return
            if self.socket_path:
                last_error: OSError | None = None
                # systemd starts the bridge before the gateway, but the Unix
                # socket may appear a fraction later.  Retry here instead of
                # falling back to a gateway-owned Devin process.
                for attempt in range(20):
                    try:
                        reader, writer = await asyncio.open_unix_connection(
                            self.socket_path
                        )
                    except OSError as exc:
                        last_error = exc
                        if attempt == 19:
                            break
                        await asyncio.sleep(0.25)
                    else:
                        self._socket_reader = reader
                        self._socket_writer = writer
                        self._reader_task = asyncio.create_task(
                            self._read_loop()
                        )
                        log.info(
                            "Connected to persistent Devin ACP bridge: %s",
                            self.socket_path,
                        )
                        return
                raise DevinAcpError(
                    f"Cannot connect to Devin ACP bridge {self.socket_path}: "
                    f"{last_error}"
                ) from last_error
            env = os.environ.copy()
            if self.home:
                env["HOME"] = self.home
                env["PATH"] = f"{self.home}/.local/bin:" + env.get("PATH", "")
            self._proc = await asyncio.create_subprocess_exec(
                *self._command(),
                cwd=self.cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._reader_task = asyncio.create_task(self._read_loop())
            self._stderr_task = asyncio.create_task(self._stderr_loop())
            try:
                await self.request("initialize", {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {"name": "g-memory-mcp", "version": "1.0"},
                }, timeout=30)
            except Exception:
                await self.close()
                raise
            log.info("Devin ACP started: model=%s", self.model)

    async def close(self) -> None:
        writer, self._socket_writer = self._socket_writer, None
        self._socket_reader = None
        proc, self._proc = self._proc, None
        current = asyncio.current_task()
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        for task in (self._reader_task, self._stderr_task):
            if task and task is not current:
                task.cancel()
        self._reader_task = self._stderr_task = None
        error = DevinAcpError(
            "Devin ACP bridge disconnected"
            if self.socket_path else "Devin ACP process closed"
        )
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    async def _send(self, payload: dict[str, Any]) -> None:
        wire = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        if self.socket_path:
            if not self.running or not self._socket_writer:
                raise DevinAcpError("Devin ACP bridge is not connected")
            self._socket_writer.write(wire.encode("utf-8"))
            await self._socket_writer.drain()
            return
        if not self.running or not self._proc or not self._proc.stdin:
            raise DevinAcpError("Devin ACP is not running")
        self._proc.stdin.write(wire.encode("utf-8"))
        await self._proc.stdin.drain()

    async def request(
        self, method: str, params: dict[str, Any], *, timeout: float = 300
    ) -> dict[str, Any]:
        if not self.running and method != "initialize":
            await self.start()
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({
            "jsonrpc": "2.0", "id": request_id,
            "method": method, "params": params,
        })
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result if isinstance(result, dict) else {"value": result}
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if not self.running:
            await self.start()
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def new_session(
        self,
        callback: EventCallback | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        result = await self.request("session/new", {
            "cwd": self.cwd,
            "mcpServers": mcp_servers or [],
        })
        session_id = str(result.get("sessionId") or result.get("session_id") or "")
        if not session_id:
            raise DevinAcpError("session/new did not return a sessionId")
        if callback:
            self._callbacks[session_id] = callback
        await self._configure_and_verify(session_id, result)
        return result

    async def load_session(
        self,
        session_id: str,
        callback: EventCallback | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if callback:
            self._callbacks[session_id] = callback
        result = await self.request("session/load", {
            "sessionId": session_id,
            "cwd": self.cwd,
            "mcpServers": mcp_servers or [],
        })
        await self._configure_and_verify(session_id, result)
        return result

    async def _configure_and_verify(
        self, session_id: str, result: dict[str, Any]
    ) -> None:
        model_option = find_config_option(result, "model")
        if not model_option:
            # --model remains authoritative on CLI builds that do not expose
            # dynamic config options.
            return
        current = _option_current(model_option)
        if current and self.model.casefold() != current.casefold():
            values = model_option.get("options") or model_option.get("values") or []
            available = []
            for value in values if isinstance(values, list) else []:
                if isinstance(value, dict):
                    available.append(str(value.get("value") or value.get("id") or ""))
                else:
                    available.append(str(value))
            if self.model in available:
                updated = await self.set_config(
                    session_id, _option_id(model_option), self.model
                )
                updated_option = find_config_option(updated, "model")
                updated_current = (
                    _option_current(updated_option) if updated_option else ""
                )
                if not updated_current or (
                    updated_current.casefold() == self.model.casefold()
                ):
                    return
                current = updated_current
            raise DevinModelUnavailable(
                f"Devin 实际启用的是 {current}，不是 {self.model}；"
                "当前订阅尚未开放目标模型"
            )

    async def set_config(self, session_id: str, config_id: str, value: str) -> dict[str, Any]:
        return await self.request("session/set_config_option", {
            "sessionId": session_id,
            "configId": config_id,
            "value": value,
        }, timeout=30)

    async def set_effort(
        self, session_id: str, effort: str, config_result: dict[str, Any]
    ) -> bool:
        option = (find_config_option(config_result, "thought_level")
                  or find_config_option(config_result, "effort")
                  or find_config_option(config_result, "thinking"))
        if not option:
            return False
        values = option.get("options") or option.get("values") or []
        available: list[str] = []
        for value in values if isinstance(values, list) else []:
            if isinstance(value, dict):
                available.append(str(value.get("value") or value.get("id") or ""))
            else:
                available.append(str(value))
        aliases = {
            "low": ("low", "small"),
            "medium": ("medium", "med"),
            "high": ("high", "large"),
            "max": ("max", "xhigh", "maximum"),
        }
        target = next((candidate for candidate in aliases.get(effort, (effort,))
                       if candidate in available), effort)
        await self.set_config(session_id, _option_id(option), target)
        return True

    async def prompt(
        self, session_id: str, text: str, callback: EventCallback | None = None
    ) -> DevinPromptResult:
        async with self._prompt_lock:
            # Install the callback only after this prompt owns the foreground
            # slot. Otherwise a queued user turn can steal updates from an
            # in-flight patrol/keepalive turn on the same Devin session.
            if callback:
                self._callbacks[session_id] = callback
            result = await self.request("session/prompt", {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": text}],
            }, timeout=900)
            if self.trace_usage:
                _trace_usage("session/prompt.result", session_id, result)
        return DevinPromptResult(
            stop_reason=str(result.get("stopReason") or ""),
            usage=result.get("usage") if isinstance(result.get("usage"), dict) else {},
            raw=result,
        )

    async def cancel(self, session_id: str) -> None:
        await self.notify("session/cancel", {"sessionId": session_id})

    def forget(self, session_id: str) -> None:
        self._callbacks.pop(session_id, None)

    async def _dispatch(self, params: dict[str, Any]) -> None:
        update = normalize_session_update(params)
        session_id = str(update.get("sessionId") or params.get("sessionId") or "")
        if self.trace_usage:
            kind = (
                update.get("sessionUpdate")
                or update.get("session_update")
                or update.get("type")
            )
            if kind == "usage_update":
                _trace_usage("session/update.usage_update", session_id, params)
        callback = self._callbacks.get(session_id) or self._event_callback
        if callback:
            value = callback(update)
            if inspect.isawaitable(value):
                await value

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "session/request_permission":
            options = params.get("options") or []
            option_id = None
            for option in options if isinstance(options, list) else []:
                if not isinstance(option, dict):
                    continue
                kind = str(option.get("kind") or option.get("name") or "")
                if kind in ("allow_once", "allow_always"):
                    option_id = option.get("optionId") or option.get("id")
                    break
            result = {"outcome": {"outcome": "selected", "optionId": option_id}} if option_id else {
                "outcome": {"outcome": "cancelled"}
            }
        else:
            result = {}
        await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _read_loop(self) -> None:
        if self.socket_path:
            assert self._socket_reader
            reader = self._socket_reader
        else:
            assert self._proc and self._proc.stdout
            reader = self._proc.stdout
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    if self.socket_path and self._socket_writer is None:
                        # The gateway is shutting down normally.  Closing its
                        # socket must be quiet and must not imply ACP failure.
                        return
                    raise DevinAcpError(
                        "Devin ACP bridge disconnected"
                        if self.socket_path else "Devin ACP stdout closed"
                    )
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("Ignoring non-JSON Devin ACP stdout: %r", raw[:300])
                    continue
                if "id" in message and ("result" in message or "error" in message):
                    future = self._pending.get(message["id"])
                    if future and not future.done():
                        if message.get("error"):
                            future.set_exception(DevinAcpError(str(message["error"])))
                        else:
                            future.set_result(message.get("result", {}))
                elif "id" in message and message.get("method"):
                    await self._handle_server_request(message)
                elif message.get("method") == "session/update":
                    params = message.get("params")
                    if isinstance(params, dict):
                        await self._dispatch(params)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.exception("Devin ACP reader stopped: %s", exc)
            if self._proc or self._socket_writer:
                asyncio.create_task(self.close())

    async def _stderr_loop(self) -> None:
        assert self._proc and self._proc.stderr
        try:
            while True:
                raw = await self._proc.stderr.readline()
                if not raw:
                    return
                log.info("Devin ACP: %s", raw.decode("utf-8", "replace").rstrip())
        except asyncio.CancelledError:
            return
