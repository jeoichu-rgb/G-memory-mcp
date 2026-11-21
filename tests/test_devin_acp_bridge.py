import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devin_acp import DevinAcpClient
from devin_acp_bridge import DevinAcpBridge


class _FakePersistentAcp:
    def __init__(self):
        self.requests = []
        self.notifications = []
        self.started = False
        self.closed = False
        self._event_callback = None
        self.replay_update = None

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True

    async def request(self, method, params, timeout=300):
        self.requests.append((method, params, timeout))
        if method == "session/load" and self.replay_update:
            await self._event_callback(self.replay_update)
        if method == "session/prompt" and self._event_callback:
            await self._event_callback({
                "sessionId": params["sessionId"],
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "alive"},
            })
            return {"stopReason": "end_turn"}
        return {"sessionId": params.get("sessionId", "persistent-session")}

    async def notify(self, method, params):
        self.notifications.append((method, params))


class DevinAcpBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.socket_path = str(Path(self.temp.name) / "devin.sock")
        self.tcp_port = None

        async def start_loopback_server(callback, path, **kwargs):
            del path
            server = await asyncio.start_server(callback, "127.0.0.1", 0, **kwargs)
            self.tcp_port = server.sockets[0].getsockname()[1]
            return server

        async def open_loopback_connection(path, **kwargs):
            del path
            return await asyncio.open_connection("127.0.0.1", self.tcp_port, **kwargs)

        # This execution sandbox disallows AF_UNIX sockets.  Exercise the
        # exact same stream protocol over loopback TCP; production still uses
        # asyncio's Unix-socket functions above.
        self.start_patch = patch(
            "devin_acp_bridge.asyncio.start_unix_server",
            side_effect=start_loopback_server,
        )
        self.open_patch = patch(
            "devin_acp.asyncio.open_unix_connection",
            side_effect=open_loopback_connection,
        )
        self.chmod_patch = patch("devin_acp_bridge.os.chmod")
        self.start_patch.start()
        self.open_patch.start()
        self.chmod_patch.start()
        self.bridge = DevinAcpBridge(
            socket_path=self.socket_path,
            binary="devin",
            model="model",
            cwd=self.temp.name,
        )
        fake = _FakePersistentAcp()
        fake._event_callback = self.bridge._broadcast_update
        self.bridge._client = fake
        self.fake = fake
        await self.bridge.start()

    async def asyncTearDown(self):
        await self.bridge.close()
        self.chmod_patch.stop()
        self.open_patch.stop()
        self.start_patch.stop()
        self.temp.cleanup()

    def client(self):
        return DevinAcpClient(
            binary="unused",
            model="model",
            cwd=self.temp.name,
            socket_path=self.socket_path,
        )

    def test_gateway_constructor_uses_bridge_environment(self):
        with patch.dict(os.environ, {"DEVIN_ACP_SOCKET": self.socket_path}):
            client = DevinAcpClient(
                binary="unused", model="model", cwd=self.temp.name
            )
        self.assertEqual(client.socket_path, self.socket_path)

    async def test_gateway_reconnect_does_not_restart_or_load_acp(self):
        first = self.client()
        created = await first.new_session()
        session_id = created["sessionId"]
        await first.close()

        second = self.client()
        chunks = []

        async def collect(update):
            chunks.append(update["content"]["text"])

        result = await second.prompt(session_id, "after restart", collect)
        await second.close()

        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(chunks, ["alive"])
        self.assertTrue(self.fake.started)
        self.assertFalse(self.fake.closed)
        self.assertEqual(
            [method for method, _params, _timeout in self.fake.requests],
            ["session/new", "session/prompt"],
        )
        self.assertNotIn(
            "session/load",
            [method for method, _params, _timeout in self.fake.requests],
        )

    async def test_bridge_returns_initialize_without_forwarding_it(self):
        reader, writer = await asyncio.open_unix_connection(self.socket_path)
        writer.write((json.dumps({
            "jsonrpc": "2.0",
            "id": 7,
            "method": "initialize",
            "params": {},
        }) + "\n").encode())
        await writer.drain()
        reply = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        self.assertEqual(reply["id"], 7)
        self.assertEqual(reply["result"]["protocolVersion"], 1)
        self.assertEqual(self.fake.requests, [])

    async def test_large_replay_and_prompt_survive_both_bridge_directions(self):
        text = "[context-forge]" + "恢复材料" * 4200
        replay = {
            "sessionId": "large-session",
            "update": {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": "text", "text": text},
                "_meta": {"cognition.ai/message": text},
            },
        }
        self.assertGreater(len(json.dumps(replay, ensure_ascii=False).encode()), 65536)
        self.fake.replay_update = replay
        client = self.client()
        updates = []

        async def collect(update):
            updates.append(update)

        try:
            with self.assertLogs("cc-gw.devin", level="INFO") as captured:
                await client.load_session("large-session", collect)
                # This request also exceeds the old bridge listener limit.
                await client.prompt("large-session", text * 2, collect)
            self.assertEqual(updates[0]["content"]["text"], text)
            self.assertEqual(updates[0]["_meta"]["cognition.ai/message"], text)
            self.assertEqual([r[0] for r in self.fake.requests], ["session/load", "session/prompt"])
            self.assertEqual(self.fake.requests[1][1]["prompt"][0]["text"], text * 2)
            logs = "\n".join(captured.output)
            self.assertIn("active_methods=session/load", logs)
            self.assertIn("meta_message_bytes=", logs)
            self.assertIn("text_sha256=", logs)
            self.assertNotIn("恢复材料", logs)
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
