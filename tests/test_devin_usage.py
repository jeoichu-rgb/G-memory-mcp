import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from devin_usage import (
    DevinUsageProbe,
    DevinUsageProbeError,
    parse_devin_usage,
    strip_terminal_controls,
)


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

    def test_probe_waits_for_tmux_prompt(self):
        probe = DevinUsageProbe(
            binary="/home/erik/.local/bin/devin",
            model="claude-opus-4-6-thinking",
            cwd="/home/erik",
        )
        probe._capture = Mock(side_effect=[
            "Claude Opus 4.6 Thinking  Starting session...",
            "Ask Devin to build features, fix bugs, or work on your code",
        ])
        with patch("devin_usage.time.sleep"):
            screen = probe._wait_until_ready(1)
        self.assertIn("Ask Devin to build features", screen)

    def test_probe_reports_directory_trust_prompt(self):
        probe = DevinUsageProbe(
            binary="/home/erik/.local/bin/devin",
            model="claude-opus-4-6-thinking",
            cwd="/opt/G-memory-mcp",
        )
        probe._capture = Mock(
            return_value="Do you trust the authors of this directory?"
        )
        with self.assertRaisesRegex(DevinUsageProbeError, "trust confirmation"):
            probe._wait_until_ready(1)

    def test_query_reads_usage_from_reused_tmux_session(self):
        probe = DevinUsageProbe(
            binary="/home/erik/.local/bin/devin",
            model="claude-opus-4-6-thinking",
            cwd="/home/erik",
        )
        probe._start = Mock()
        probe._wait_until_ready = Mock(
            return_value="Ask Devin to build features, fix bugs, or work on your code"
        )
        probe._send_literal = Mock()
        probe._send_enter = Mock()
        probe._capture = Mock(return_value=(
            "❭ /usage\nUsage\n"
            "Daily 2% used · resets in 5h 28m\n"
            "Weekly 1% used · resets Nov 15, 8:00 AM (UTC)\n"
        ))
        with patch("devin_usage.time.sleep"):
            result = probe._query_once()
        probe._send_literal.assert_called_once_with("/usage")
        probe._send_enter.assert_called_once_with()
        self.assertEqual([w["key"] for w in result["windows"]], ["daily", "weekly"])

    def test_terminal_control_stripping(self):
        self.assertEqual(strip_terminal_controls("\x1b[31mDaily\x1b[0m\r"), "Daily\n")


if __name__ == "__main__":
    unittest.main()
