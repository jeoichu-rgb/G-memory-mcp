import json
import tempfile
import unittest
from pathlib import Path

from devin_mcp_config import (
    DEFAULT_PALACE_SERVER,
    DevinMcpConfigError,
    filter_devin_mcp_servers,
    read_configured_mcp_servers,
    read_devin_mcp_allowlist,
    resolve_server_name,
    write_devin_mcp_allowlist,
)


class DevinMcpConfigTests(unittest.TestCase):
    def test_missing_state_defaults_to_palace_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allowlist.json"
            self.assertEqual(
                read_devin_mcp_allowlist(path),
                [DEFAULT_PALACE_SERVER],
            )

    def test_allowlist_round_trip_is_private_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "allowlist.json"
            written = write_devin_mcp_allowlist(
                path,
                [DEFAULT_PALACE_SERVER, "coread", DEFAULT_PALACE_SERVER],
            )
            self.assertEqual(written, [DEFAULT_PALACE_SERVER, "coread"])
            self.assertEqual(read_devin_mcp_allowlist(path), written)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allowlist.json"
            path.write_text('{"enabled": "palace"}', encoding="utf-8")
            with self.assertRaises(DevinMcpConfigError):
                read_devin_mcp_allowlist(path)

    def test_project_local_config_overrides_project_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / ".claude"
            config_dir.mkdir()
            (config_dir / "settings.json").write_text(json.dumps({
                "mcpServers": {
                    DEFAULT_PALACE_SERVER: {"url": "https://old.example/sse"},
                    "claude_ai_coreading": {"url": "https://read.example/sse"},
                }
            }), encoding="utf-8")
            (config_dir / "settings.local.json").write_text(json.dumps({
                "mcpServers": {
                    DEFAULT_PALACE_SERVER: {
                        "url": "https://palace.example/sse",
                        "headers": {"x-secret": "preserved"},
                    }
                }
            }), encoding="utf-8")

            configured = read_configured_mcp_servers(root)
            selected = filter_devin_mcp_servers(
                configured, [DEFAULT_PALACE_SERVER]
            )

            self.assertEqual(list(selected), [DEFAULT_PALACE_SERVER])
            self.assertEqual(
                selected[DEFAULT_PALACE_SERVER]["headers"]["x-secret"],
                "preserved",
            )
            self.assertEqual(
                selected[DEFAULT_PALACE_SERVER]["url"],
                "https://palace.example/sse",
            )

    def test_friendly_aliases_resolve_to_configured_names(self):
        available = [DEFAULT_PALACE_SERVER, "claude_ai_coreading", "stockscanner"]
        self.assertEqual(
            resolve_server_name("palace", available), DEFAULT_PALACE_SERVER
        )
        self.assertEqual(
            resolve_server_name("coread", available), "claude_ai_coreading"
        )
        self.assertEqual(
            resolve_server_name("stock", available), "stockscanner"
        )


if __name__ == "__main__":
    unittest.main()
