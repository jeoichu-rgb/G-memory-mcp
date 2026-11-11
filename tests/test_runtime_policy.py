import unittest

from runtime_policy import subscription_only_task_blocked


class RuntimePolicyTests(unittest.TestCase):
    def test_openrouter_blocks_subscription_only_task(self):
        self.assertTrue(subscription_only_task_blocked("openrouter"))

    def test_subscription_and_unbound_runtime_remain_allowed(self):
        self.assertFalse(subscription_only_task_blocked("subscription"))
        self.assertFalse(subscription_only_task_blocked(None))

    def test_custom_api_runtime_name_can_be_supplied(self):
        self.assertTrue(subscription_only_task_blocked("api", "api"))


if __name__ == "__main__":
    unittest.main()
