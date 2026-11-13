import unittest
from datetime import datetime, timezone

from devin_usage import DevinUsageProbe, parse_devin_usage, strip_terminal_controls


class DevinUsageParsingTests(unittest.TestCase):
    def test_daily_weekly_usage_and_resets(self):
        captured = datetime(2026, 11, 13, 1, 19, tzinfo=timezone.utc)
        parsed = parse_devin_usage(
            """
Usage
Daily   ■■■■■■■■■■■■■■■■■■■■  2% used  · resets in 6h 41m
Weekly  ■■■■■■■■■■■■■■■■■■■■  1% used  · resets Nov 15, 8:00 AM (UTC)
""",
            captured,
        )
        self.assertEqual(parsed["source"], "devin-cli-/usage")
        self.assertEqual(parsed["windows"], [
            {
                "key": "daily",
                "utilization": 2.0,
                "resets_at": "2026-11-13T08:00:00Z",
                "reset_text": "in 6h 41m",
            },
            {
                "key": "weekly",
                "utilization": 1.0,
                "resets_at": "2026-11-15T08:00:00Z",
                "reset_text": "Nov 15, 8:00 AM (UTC)",
            },
        ])

    def test_ansi_sequences_and_redraws_are_ignored(self):
        captured = datetime(2026, 11, 13, 1, 19, tzinfo=timezone.utc)
        parsed = parse_devin_usage(
            "\x1b[2JUsage\r\x1b[32mDaily\x1b[0m   2% used · resets in 6h 41m\r"
            "Weekly   1% used · resets Nov 15, 8:00 AM (UTC)\r",
            captured,
        )
        self.assertEqual(parsed["windows"][0]["utilization"], 2.0)
        self.assertEqual(parsed["windows"][1]["utilization"], 1.0)

    def test_probe_launches_as_erik_with_exact_model(self):
        probe = DevinUsageProbe(
            binary="/home/erik/.local/bin/devin",
            model="claude-opus-4-6-thinking",
            cwd="/opt/G-memory-mcp",
            run_as_user="erik",
            home="/home/erik",
        )
        command = probe._command()
        self.assertEqual(command[-3:], [
            "/home/erik/.local/bin/devin",
            "--model",
            "claude-opus-4-6-thinking",
        ])
        self.assertIn("/home/erik/.local/bin/devin", command)

    def test_auth_status_uses_same_erik_identity(self):
        probe = DevinUsageProbe(
            binary="/home/erik/.local/bin/devin",
            model="claude-opus-4-6-thinking",
            cwd="/opt/G-memory-mcp",
            run_as_user="erik",
        )
        command = probe._run_as_user([
            "/home/erik/.local/bin/devin", "auth", "status"
        ])
        self.assertEqual(command[-3:], [
            "/home/erik/.local/bin/devin", "auth", "status"
        ])

    def test_terminal_control_stripping(self):
        self.assertEqual(strip_terminal_controls("\x1b[31mDaily\x1b[0m\r"), "Daily\n")


if __name__ == "__main__":
    unittest.main()
