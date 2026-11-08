import os
import unittest

from openrouter_cache import (
    OpenRouterCacheConfig,
    merge_custom_headers,
    prepare_openrouter_payload,
)


class OpenRouterCacheTests(unittest.TestCase):
    def config(self):
        return OpenRouterCacheConfig(
            session_id="jeoi-erik-main",
            ttl="1h",
            provider_only=("anthropic",),
            allow_fallbacks=True,
            proxy_base_url="http://127.0.0.1:3000/openrouter",
            upstream_base_url="https://openrouter.ai/api",
        )

    def test_places_exact_system_and_second_last_user_breakpoints(self):
        payload = {
            "system": [{
                "type": "text",
                "text": "stable",
            }],
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "reply"},
                {"role": "user", "content": "latest"},
            ],
            "cache_control": {"type": "ephemeral"},
        }
        result, count = prepare_openrouter_payload(payload, self.config())
        self.assertEqual(count, 2)
        self.assertEqual(
            result["system"][0]["cache_control"],
            {"type": "ephemeral", "ttl": "1h"},
        )
        self.assertEqual(
            result["messages"][0]["content"][0]["cache_control"],
            {"type": "ephemeral", "ttl": "1h"},
        )
        self.assertNotIn("cache_control", result)
        self.assertIsInstance(result["messages"][-1]["content"], str)

    def test_first_turn_only_has_stable_system_breakpoint(self):
        result, count = prepare_openrouter_payload(
            {
                "system": "stable persona",
                "messages": [{"role": "user", "content": "hello"}],
            },
            self.config(),
        )
        self.assertEqual(count, 1)
        self.assertEqual(result["session_id"], "jeoi-erik-main")
        self.assertEqual(result["system"][0]["cache_control"]["ttl"], "1h")
        self.assertNotIn("cache_control", result)
        self.assertEqual(result["provider"]["only"], ["anthropic"])
        self.assertTrue(result["provider"]["allow_fallbacks"])

    def test_tool_results_and_attachments_do_not_advance_breakpoint(self):
        payload = {
            "system": "stable",
            "messages": [
                {"role": "user", "content": "plain one"},
                {"role": "assistant", "content": "using tool"},
                {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": "t1", "content": "ok",
                }]},
                {"role": "assistant", "content": "tool done"},
                {"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "data": "x"}},
                    {"type": "text", "text": "image question"},
                ]},
                {"role": "assistant", "content": "image reply"},
                {"role": "user", "content": "plain latest"},
            ],
        }
        result, count = prepare_openrouter_payload(payload, self.config())
        self.assertEqual(count, 2)
        self.assertEqual(
            result["messages"][0]["content"][0]["cache_control"]["ttl"],
            "1h",
        )
        self.assertNotIn(
            "cache_control", result["messages"][2]["content"][0]
        )
        self.assertNotIn(
            "cache_control", result["messages"][4]["content"][0]
        )

    def test_removes_routing_overrides_that_defeat_stickiness(self):
        result, _ = prepare_openrouter_payload(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "provider": {
                    "order": ["anthropic"],
                    "sort": "price",
                    "data_collection": "deny",
                },
            },
            self.config(),
        )
        self.assertNotIn("order", result["provider"])
        self.assertNotIn("sort", result["provider"])
        self.assertEqual(result["provider"]["data_collection"], "deny")

    def test_custom_header_keeps_one_session_id(self):
        value = merge_custom_headers(
            "Accept-Language: zh-CN\nx-session-id: stale\n"
            "x-g-memory-proxy-secret: old",
            "stable",
            "new-secret",
        )
        self.assertEqual(
            value,
            "Accept-Language: zh-CN\nx-session-id: stable\n"
            "x-g-memory-proxy-secret: new-secret",
        )


if __name__ == "__main__":
    unittest.main()
