import unittest

from devin_acp import (
    DevinAcpClient,
    DevinModelUnavailable,
    extract_usage,
    find_config_option,
    normalize_session_update,
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


if __name__ == "__main__":
    unittest.main()
