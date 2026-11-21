import unittest
import asyncio
import sys

from devin_acp import (
    DevinAcpClient,
    DevinModelUnavailable,
    cache_read_tokens,
    extract_usage,
    find_config_option,
    normalize_session_update,
    normalize_mcp_servers,
)


class DevinAcpParsingTests(unittest.TestCase):
    def test_standard_session_update_envelope_is_flattened(self):
        update = normalize_session_update({
            "sessionId": "towering-munchkin",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "hello"},
            },
        })
        self.assertEqual(update["sessionId"], "towering-munchkin")
        self.assertEqual(update["sessionUpdate"], "agent_message_chunk")
        self.assertEqual(update["content"]["text"], "hello")

    def test_usage_update_reads_devin_metadata_and_cost(self):
        usage = extract_usage({
            "sessionUpdate": "usage_update",
            "used": 10881,
            "size": 262144,
            "cost": {"amount": 0.1234, "currency": "USD"},
            "_meta": {
                "cognition.ai/inputTokens": 10859,
                "cognition.ai/outputTokens": 22,
                "cognition.ai/cachedInputTokens": 9000,
            },
        })
        self.assertEqual(usage["context_used"], 10881)
        self.assertEqual(usage["context_size"], 262144)
        self.assertEqual(usage["input_tokens"], 10859)
        self.assertEqual(usage["output_tokens"], 22)
        self.assertEqual(usage["cached_input_tokens"], 9000)
        self.assertEqual(usage["cost"], 0.1234)

    def test_model_config_option_is_found(self):
        option = find_config_option({
            "configOptions": [{
                "id": "model",
                "category": "model",
                "currentValue": "claude-opus-4-6-thinking",
            }]
        }, "model")
        self.assertIsNotNone(option)
        self.assertEqual(option["currentValue"], "claude-opus-4-6-thinking")

    def test_cache_reads_distinguish_zero_from_missing(self):
        self.assertEqual(cache_read_tokens({"cache_read_input_tokens": 0}), 0)
        self.assertEqual(extract_usage({"cachedInputTokens": 0})["cached_input_tokens"], 0)
        self.assertIsNone(extract_usage({"used": 20000})["cached_input_tokens"])

    def test_cache_reads_from_devin_prompt_result_and_usage_update(self):
        for cached in (31108, 0):
            with self.subTest(cached=cached):
                self.assertEqual(cache_read_tokens({
                    "inputTokens": 31466,
                    "cachedReadTokens": cached,
                    "cachedWriteTokens": 355,
                }), cached)
                self.assertEqual(extract_usage({
                    "sessionUpdate": "usage_update",
                    "_meta": {
                        "cognition.ai/cachedReadTokens": cached,
                        "cognition.ai/cachedWriteTokens": 355,
                    },
                })["cached_input_tokens"], cached)
        self.assertIsNone(cache_read_tokens({"cachedWriteTokens": 31108}))

    def test_sse_mcp_has_required_empty_headers(self):
        servers = normalize_mcp_servers({
            "tools": {
                "type": "sse",
                "url": "http://127.0.0.1:8001/mcp/example/sse",
            },
        })
        self.assertEqual(servers, [{
            "name": "tools",
            "type": "sse",
            "url": "http://127.0.0.1:8001/mcp/example/sse",
            "headers": [],
        }])

    def test_stdio_mcp_uses_v1_untagged_shape(self):
        servers = normalize_mcp_servers({
            "local": {"command": "python3", "args": ["server.py"]},
        })
        self.assertEqual(servers, [{
            "name": "local",
            "command": "python3",
            "args": ["server.py"],
            "env": [],
        }])
        self.assertNotIn("type", servers[0])

    def test_root_launches_devin_as_erik(self):
        client = DevinAcpClient(
            binary="/home/erik/.local/bin/devin",
            model="claude-opus-4-6-thinking",
            cwd="/opt/G-memory-mcp",
            run_as_user="erik",
            home="/home/erik",
        )
        command = client._command()
        # CI normally runs as root. On a non-root workstation the command is
        # intentionally direct, so assert the invariant at the tail.
        self.assertEqual(
            command[-4:],
            ["/home/erik/.local/bin/devin", "acp", "--model",
             "claude-opus-4-6-thinking"],
        )


class DevinModelGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_free_tier_fallback_is_rejected(self):
        client = DevinAcpClient(
            binary="devin",
            model="claude-opus-4-6-thinking",
            cwd="/tmp",
        )
        with self.assertRaises(DevinModelUnavailable):
            await client._configure_and_verify("sid", {
                "configOptions": [{
                    "id": "model",
                    "category": "model",
                    "currentValue": "swe-2-flash-medium",
                    "options": [
                        {"value": "swe-2-flash-medium"},
                        {"value": "swe-1-6-slow"},
                    ],
                }]
            })

    async def test_fusion_model_is_not_accepted_as_plain_opus(self):
        client = DevinAcpClient(
            binary="devin",
            model="claude-opus-4-6-thinking",
            cwd="/tmp",
        )
        with self.assertRaises(DevinModelUnavailable):
            await client._configure_and_verify("sid", {
                "configOptions": [{
                    "id": "model",
                    "category": "model",
                    "currentValue":
                        "fusion-claude-opus-4-6-thinking-sidekick-haiku",
                    "options": [],
                }]
            })


class DevinPromptSerializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_subprocess_load_replays_large_frame_without_prompt(self):
        client = DevinAcpClient(binary="unused", model="model", cwd="/tmp")
        client.socket_path = ""
        script = '''
import json, sys
text = "[context-forge]" + "恢复材料" * 4200
for line in sys.stdin:
    req = json.loads(line)
    if req["method"] == "session/load":
        print(json.dumps({"jsonrpc":"2.0", "method":"session/update", "params":{
            "sessionId":"large-session", "update":{
                "sessionUpdate":"user_message_chunk",
                "content":{"type":"text", "text":text},
                "_meta":{"cognition.ai/message":text}
            }}}, ensure_ascii=False), flush=True)
    print(json.dumps({"jsonrpc":"2.0", "id":req["id"], "result":{
        "sessionId":"large-session"
    }}), flush=True)
'''
        client._command = lambda: [sys.executable, "-u", "-c", script]
        updates = []

        async def collect(update):
            updates.append(update)

        try:
            with self.assertLogs("cc-gw.devin", level="INFO") as captured:
                await asyncio.wait_for(client.load_session("large-session", collect), 5)
            self.assertEqual(len(updates), 1)
            self.assertEqual(updates[0]["content"]["text"], "[context-forge]" + "恢复材料" * 4200)
            logs = "\n".join(captured.output)
            self.assertIn("transport=process", logs)
            self.assertIn("direction=receive", logs)
            self.assertIn("active_methods=session/load", logs)
            self.assertNotIn("method=session/prompt", logs)
            self.assertNotIn("恢复材料", logs)
            self.assertTrue(client.running)
        finally:
            if client._reader_task:
                client._reader_task.cancel()
            await client.close()

    async def test_queued_prompt_does_not_steal_active_callback(self):
        client = DevinAcpClient(
            binary="devin", model="model", cwd="/tmp"
        )
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls = 0

        async def fake_request(method, params, timeout=300):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_started.set()
                await release_first.wait()
            return {"stopReason": "end_turn"}

        client.request = fake_request

        async def first_callback(update):
            return None

        async def second_callback(update):
            return None

        first = asyncio.create_task(
            client.prompt("session", "first", first_callback)
        )
        await first_started.wait()
        second = asyncio.create_task(
            client.prompt("session", "second", second_callback)
        )
        await asyncio.sleep(0)
        self.assertIs(client._callbacks["session"], first_callback)

        release_first.set()
        await first
        await second
        self.assertIs(client._callbacks["session"], second_callback)


if __name__ == "__main__":
    unittest.main()
