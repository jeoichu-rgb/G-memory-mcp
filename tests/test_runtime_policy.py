import unittest

from runtime_policy import (
    forge_route,
    session_route_change_locked,
    subscription_only_task_blocked,
)


class RuntimePolicyTests(unittest.TestCase):
    def test_openrouter_blocks_subscription_only_task(self):
        self.assertTrue(subscription_only_task_blocked("openrouter"))

    def test_subscription_and_unbound_runtime_remain_allowed(self):
        self.assertFalse(subscription_only_task_blocked("subscription"))
        self.assertFalse(subscription_only_task_blocked(None))

    def test_custom_api_runtime_name_can_be_supplied(self):
        self.assertTrue(subscription_only_task_blocked("api", "api"))

    def test_forge_keeps_each_source_runtime_and_verifier(self):
        cases = (
            ("subscription", "claude-opus-4-6", "claude-tmux"),
            ("openrouter", "openrouter:anthropic/claude-opus-4.6", "claude-tmux"),
            ("devin", "devin:claude-opus-4-6-thinking", "devin-acp"),
        )
        for runtime, model, verifier in cases:
            with self.subTest(runtime=runtime):
                route = forge_route(runtime, model)
                self.assertEqual(route["runtime"], runtime)
                self.assertEqual(route["model"], model)
                self.assertEqual(route["verifier"], verifier)

    def test_unknown_forge_runtime_is_rejected_instead_of_cross_routed(self):
        with self.assertRaises(ValueError):
            forge_route("mystery", "model")

    def test_bound_session_cannot_be_repointed_to_another_channel(self):
        self.assertTrue(session_route_change_locked(
            "subscription", "devin", backend_bound=True, turn_count=0
        ))
        self.assertTrue(session_route_change_locked(
            "openrouter", "subscription", backend_bound=True, turn_count=0
        ))
        self.assertTrue(session_route_change_locked(
            "devin", "devin", backend_bound=True, turn_count=0
        ))
        self.assertFalse(session_route_change_locked(
            "subscription", "subscription", backend_bound=True, turn_count=4
        ))
        self.assertFalse(session_route_change_locked(
            "subscription", "devin", backend_bound=False, turn_count=0
        ))


if __name__ == "__main__":
    unittest.main()
