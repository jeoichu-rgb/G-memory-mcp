import os
import unittest

from openrouter_cache import (
    OpenRouterCacheConfig,
    apply_output_token_cap,
    build_keepalive_payload,
    merge_custom_headers,
    prepare_openrouter_payload,
    sticky_session_id,
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

    def test_places_system_and_completed_dialogue_breakpoints(self):
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
            result["messages"][1]["content"][0]["cache_control"],
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
            result["messages"][5]["content"][0]["cache_control"]["ttl"],
            "1h",
        )
        self.assertNotIn(
            "cache_control", result["messages"][2]["content"][0]
        )
        self.assertNotIn(
            "cache_control", result["messages"][4]["content"][0]
        )

    def test_forge_inheritance_is_fixed_before_progressive_dialogue(self):
        payload = {
            "system": "stable",
            "messages": [
                {"role": "user", "content": (
                    "═══ inherited_context · 上一 Session 总日记（非Jeoi新消息） ═══\n"
                    "diary\n"
                    "═══ inherited_context 结束；下面接 Forge 保留的最近原始对话 ═══"
                )},
                {"role": "user", "content": "retained old dialogue"},
                {"role": "assistant", "content": "retained reply"},
                {"role": "user", "content": (
                    "[forge-verify] 这不是Jeoi的消息——网关正在验证"
                )},
                {"role": "assistant", "content": [{
                    "type": "text", "text": "ok",
                }]},
                {"role": "user", "content": "new session first"},
                {"role": "assistant", "content": "first reply"},
                {"role": "user", "content": "new session latest"},
            ],
        }
        result, count = prepare_openrouter_payload(payload, self.config())
        self.assertEqual(count, 3)
        self.assertEqual(
            result["messages"][4]["content"][0]["cache_control"]["ttl"],
            "1h",
        )
        self.assertEqual(
            result["messages"][6]["content"][0]["cache_control"]["ttl"],
            "1h",
        )
        self.assertIsInstance(result["messages"][1]["content"], str)
        self.assertIsInstance(result["messages"][7]["content"], str)

    def test_optional_fresh_session_diary_is_split_from_first_message(self):
        injection = (
            "═══ 自动注入 · 以下是系统为你恢复的上下文，不是Jeoi的消息 ═══\n\n"
            "📖 这是你今天写的日记：\nhello\n\n"
            "═══════════════════════════════════════════════════════════════"
        )
        payload = {
            "system": "stable",
            "messages": [
                {"role": "user", "content": injection + "\n\n[time]\nfirst"},
                {"role": "assistant", "content": "reply"},
                {"role": "user", "content": "latest"},
            ],
        }
        result, count = prepare_openrouter_payload(payload, self.config())
        self.assertEqual(count, 3)
        first_blocks = result["messages"][0]["content"]
        self.assertEqual(first_blocks[0]["text"], injection)
        self.assertEqual(first_blocks[0]["cache_control"]["ttl"], "1h")
        self.assertEqual(first_blocks[1]["text"], "\n\n[time]\nfirst")
        self.assertNotIn("cache_control", first_blocks[1])
        self.assertEqual(
            result["messages"][1]["content"][0]["cache_control"]["ttl"],
            "1h",
        )

    def test_no_optional_inheritance_keeps_two_breakpoint_policy(self):
        payload = {
            "system": "stable",
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "reply"},
                {"role": "user", "content": "latest"},
            ],
        }
        _, count = prepare_openrouter_payload(payload, self.config())
        self.assertEqual(count, 2)

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

    def test_sticky_session_survives_transcript_forge(self):
        root = sticky_session_id("gateway-root")
        self.assertEqual(root, sticky_session_id("gateway-root"))
        self.assertNotEqual(root, sticky_session_id("another-chat"))
        self.assertLessEqual(len(root), 256)

    def test_keepalive_replays_only_cached_prefix(self):
        prepared, _ = prepare_openrouter_payload(
            {
                "model": "anthropic/claude-opus-4.6",
                "max_tokens": 8192,
                "thinking": {"type": "adaptive"},
                "system": "stable",
                "messages": [
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "reply"},
                    {"role": "user", "content": "live request"},
                ],
            },
            self.config(),
        )
        heartbeat = build_keepalive_payload(prepared)
        self.assertIsNotNone(heartbeat)
        self.assertFalse(heartbeat["stream"])
        self.assertEqual(heartbeat["max_tokens"], 8192)
        self.assertEqual(heartbeat["thinking"], {"type": "adaptive"})
        self.assertEqual(len(heartbeat["messages"]), 3)
        self.assertEqual(heartbeat["messages"][1]["role"], "assistant")
        self.assertIn("cache-keepalive", heartbeat["messages"][2]["content"][0]["text"])
        self.assertNotIn("live request", str(heartbeat["messages"]))

    def test_keepalive_closes_cached_tool_use_without_zero_token_hack(self):
        payload = {
            "model": "anthropic/claude-opus-4.6",
            "max_tokens": 4096,
            "system": [{
                "type": "text", "text": "stable",
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }],
            "messages": [{
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tool-1", "name": "search", "input": {}},
                    {"type": "text", "text": "checking",
                     "cache_control": {"type": "ephemeral", "ttl": "1h"}},
                ],
            }],
        }
        heartbeat = build_keepalive_payload(payload)
        blocks = heartbeat["messages"][-1]["content"]
        self.assertEqual(blocks[0]["type"], "tool_result")
        self.assertEqual(blocks[0]["tool_use_id"], "tool-1")
        self.assertNotIn("max_completion_tokens", heartbeat)
        self.assertEqual(heartbeat["max_tokens"], 4096)

    def test_output_cap_limits_adaptive_thinking(self):
        payload = {
            "max_tokens": 8192,
            "thinking": {"type": "adaptive"},
        }
        applied, reason, effective = apply_output_token_cap(payload, 20)
        self.assertTrue(applied)
        self.assertEqual(reason, "applied")
        self.assertEqual(effective, 20)
        self.assertEqual(payload["max_tokens"], 20)

    def test_output_cap_skips_incompatible_manual_thinking(self):
        payload = {
            "max_tokens": 8192,
            "thinking": {"type": "enabled", "budget_tokens": 4096},
        }
        applied, reason, effective = apply_output_token_cap(payload, 20)
        self.assertFalse(applied)
        self.assertEqual(reason, "manual_thinking_budget")
        self.assertEqual(effective, 8192)
        self.assertEqual(payload["max_tokens"], 8192)

    def test_output_cap_supports_chat_completion_field(self):
        payload = {"max_completion_tokens": 4096}
        applied, _, effective = apply_output_token_cap(payload, 800)
        self.assertTrue(applied)
        self.assertEqual(effective, 800)
        self.assertEqual(payload["max_completion_tokens"], 800)


if __name__ == "__main__":
    unittest.main()
