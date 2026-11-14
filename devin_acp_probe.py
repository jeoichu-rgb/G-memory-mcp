"""Compare Devin ACP cache telemetry in fresh, short-lived CLI sessions."""

import argparse
import asyncio
import logging
import os
import tempfile

from devin_acp import (
    DevinAcpClient,
    DevinAcpError,
    _option_current,
    _option_id,
    cache_read_tokens,
    find_config_option,
)


DEFAULT_MODELS = ("swe-2-flash-medium", "swe-1-6-slow")
KEEPALIVE = (
    "[keepalive] This is an automatic session keepalive, not a new message "
    "from Jeoi. Do not call tools. Reply with exactly OK and nothing else."
)
SEED = "Do not call tools. Read this synthetic table and reply only OK.\n" + "\n".join(
    f"Record {index:03d}: Station amber, sector {index % 17}, terminal cobalt, "
    f"inventory {index * 47}, inspection planned for the following morning."
    for index in range(240)
)
TURNS = (
    ("seed", SEED),
    ("chat", "Do not call tools. Reply only with the inventory for record 002."),
    ("keepalive", KEEPALIVE),
    ("chat-after-keepalive", "Do not call tools. Reply only with the inventory for record 003."),
)


class ProbeClient(DevinAcpClient):
    async def _handle_server_request(self, message: dict) -> None:
        if message.get("method") == "session/request_permission":
            await self._send({
                "jsonrpc": "2.0", "id": message["id"],
                "result": {"outcome": {"outcome": "cancelled"}},
            })
            return
        await super()._handle_server_request(message)


async def probe(model: str, binary: str, list_models: bool) -> None:
    with tempfile.TemporaryDirectory(prefix="devin-acp-probe-") as cwd:
        client = ProbeClient(binary=binary, model=model, cwd=cwd)
        try:
            await asyncio.wait_for(client.start(), timeout=40)
            info = await client.request("session/new", {
                "cwd": cwd, "mcpServers": [],
            }, timeout=30)
            session_id = info.get("sessionId") or info.get("session_id")
            option = find_config_option(info, "model") or {}
            options = option.get("options") or option.get("values") or []
            available = [
                (item.get("value") or item.get("id")) if isinstance(item, dict) else item
                for item in options
            ]
            current = _option_current(option)
            if not list_models and session_id and current != model and model in available:
                selected = await client.set_config(session_id, _option_id(option), model)
                current = _option_current(find_config_option(selected, "model") or {})
            print(f"MODEL requested={model} active={current}", flush=True)
            if list_models or current != model:
                print("MODEL_IDS", *filter(None, available), sep="\n", flush=True)
            if list_models:
                return
            if not session_id or current != model:
                raise DevinAcpError("Cannot verify the requested model. No prompts were sent.")

            event_reads: list[int | None] = []

            def on_update(update: dict) -> None:
                kind = (
                    update.get("sessionUpdate")
                    or update.get("session_update")
                    or update.get("type")
                )
                if kind == "usage_update":
                    event_reads.append(cache_read_tokens(update))

            for number, (label, prompt) in enumerate(TURNS, 1):
                print(f"ROUND {number} {label}", flush=True)
                event_reads.clear()
                result = await asyncio.wait_for(
                    client.prompt(session_id, prompt, on_update), timeout=90,
                )
                # Leave time to see usage notifications sent after the result.
                await asyncio.sleep(1)
                print(
                    f"SUMMARY stop={result.stop_reason} "
                    f"final_cache_read={cache_read_tokens(result.usage)} "
                    f"update_cache_reads={event_reads}",
                    flush=True,
                )
                if result.stop_reason != "end_turn":
                    raise DevinAcpError("Unexpected stop reason. Remaining prompts skipped.")
        finally:
            await client.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("models", nargs="*", metavar="MODEL_ID")
    parser.add_argument("--list-models", action="store_true")
    parser.add_argument("--binary", default="devin")
    args = parser.parse_args()
    models = args.models or DEFAULT_MODELS
    if args.list_models:
        models = models[:1]
    elif len(models) != 2 or len(set(models)) != 2:
        parser.error("provide two distinct model IDs or leave both at their defaults")

    os.environ.pop("DEVIN_ACP_SOCKET", None)
    os.environ["DEVIN_ACP_USAGE_TRACE"] = "1"
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    class ProbeLogFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return str(record.msg).startswith("Devin ACP usage trace:")

    logging.getLogger("cc-gw.devin").addFilter(ProbeLogFilter())
    failed = False
    for model in models:
        try:
            await probe(model, args.binary, args.list_models)
        except (DevinAcpError, OSError, ValueError, asyncio.TimeoutError) as exc:
            failed = True
            print(f"ERROR model={model}: {exc or type(exc).__name__}", flush=True)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
