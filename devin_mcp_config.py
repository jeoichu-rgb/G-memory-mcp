#!/usr/bin/env python3
"""Runtime allowlist and VPS CLI for MCP servers injected into Devin ACP.

The gateway passes MCP definitions directly in ACP ``session/new`` and
``session/load`` calls, so Devin's own ``devin mcp`` commands cannot manage
them.  This module keeps a separate, secret-free allowlist that is read for
every new or loaded Devin session.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Iterable


log = logging.getLogger(__name__)

DEFAULT_PALACE_SERVER = "claude_ai_Erik_tools"
DEFAULT_DEVIN_MCP_ALLOWLIST = (DEFAULT_PALACE_SERVER,)
STATE_VERSION = 1

_ALIASES = {
    "palace": DEFAULT_PALACE_SERVER,
    "erik": DEFAULT_PALACE_SERVER,
    "eriktools": DEFAULT_PALACE_SERVER,
    "coread": "claude_ai_coreading",
    "coreading": "claude_ai_coreading",
}


class DevinMcpConfigError(ValueError):
    """Raised when the runtime allowlist or a requested server is invalid."""


def default_project_root() -> Path:
    return Path(os.getenv("CC_CWD", str(Path(__file__).resolve().parent)))


def default_allowlist_path() -> Path:
    user = os.getenv("DEVIN_USER", "erik")
    home = Path(os.getenv("DEVIN_HOME", f"/home/{user}"))
    return Path(os.getenv(
        "DEVIN_MCP_ALLOWLIST_PATH",
        str(home / ".config" / "g-memory-mcp" / "devin_mcp_allowlist.json"),
    ))


def _dedupe_names(names: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_name in names:
        name = str(raw_name).strip()
        if name and name not in seen:
            result.append(name)
            seen.add(name)
    return result


def read_configured_mcp_servers(project_root: Path) -> dict[str, dict]:
    """Merge the Claude project MCP definitions used by the gateway."""
    merged: dict[str, dict] = {}
    for path in (
        project_root / ".claude" / "settings.json",
        project_root / ".claude" / "settings.local.json",
    ):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not read MCP config %s: %s", path, exc)
            continue
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            continue
        for name, config in servers.items():
            if isinstance(config, dict):
                merged[str(name)] = config
    return merged


def read_devin_mcp_allowlist(path: Path) -> list[str]:
    """Read the persisted allowlist; a missing file defaults to Palace only."""
    if not path.exists():
        return list(DEFAULT_DEVIN_MCP_ALLOWLIST)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DevinMcpConfigError(f"无法读取 Devin MCP 白名单 {path}: {exc}") from exc

    enabled = data.get("enabled") if isinstance(data, dict) else data
    if not isinstance(enabled, list) or not all(isinstance(x, str) for x in enabled):
        raise DevinMcpConfigError(
            f"Devin MCP 白名单格式错误：{path} 的 enabled 必须是字符串数组"
        )
    return _dedupe_names(enabled)


def write_devin_mcp_allowlist(path: Path, enabled: Iterable[str]) -> list[str]:
    """Atomically persist a secret-free allowlist with private permissions."""
    names = _dedupe_names(enabled)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass

    payload = {
        "version": STATE_VERSION,
        "enabled": names,
    }
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if tmp_name:
            Path(tmp_name).unlink(missing_ok=True)
    return names


def filter_devin_mcp_servers(
    servers: dict[str, dict], enabled: Iterable[str]
) -> dict[str, dict]:
    """Return configured servers whose names are present in the allowlist."""
    allowed = set(_dedupe_names(enabled))
    return {name: config for name, config in servers.items() if name in allowed}


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def resolve_server_name(query: str, available: Iterable[str]) -> str:
    """Resolve exact names plus convenient Palace/Coread/substring aliases."""
    names = _dedupe_names(available)
    if query in names:
        return query

    folded = query.casefold()
    exact_folded = [name for name in names if name.casefold() == folded]
    if len(exact_folded) == 1:
        return exact_folded[0]

    normalized_query = _normalized_name(query)
    alias_target = _ALIASES.get(normalized_query)
    if alias_target in names:
        return alias_target

    fuzzy = [
        name for name in names
        if normalized_query
        and normalized_query in _normalized_name(name)
    ]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if len(fuzzy) > 1:
        raise DevinMcpConfigError(
            f"名称 {query!r} 对应多个 MCP：{', '.join(fuzzy)}；请使用完整名称"
        )
    raise DevinMcpConfigError(
        f"找不到 MCP {query!r}。已配置：{', '.join(names) or '（无）'}"
    )


def _resolve_many(queries: Iterable[str], available: Iterable[str]) -> list[str]:
    names = list(available)
    return _dedupe_names(resolve_server_name(query, names) for query in queries)


def _print_status(
    servers: dict[str, dict], enabled: Iterable[str], state_path: Path
) -> None:
    enabled_names = _dedupe_names(enabled)
    enabled_set = set(enabled_names)
    print(f"Devin MCP 白名单：{state_path}")
    if not servers:
        print("没有在 .claude/settings*.json 中找到 MCP。")
    else:
        for name in servers:
            marker = "连接" if name in enabled_set else "禁用"
            print(f"  [{marker}] {name}")
    missing = [name for name in enabled_names if name not in servers]
    if missing:
        print("  [缺少配置] " + ", ".join(missing))
    print("只影响下一次 new/load/forge 的 Devin session；当前 session 不会热变更。")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="管理网关注入到 Devin ACP session 的 MCP 白名单",
    )
    parser.add_argument("--root", type=Path, default=default_project_root())
    parser.add_argument("--state-file", type=Path, default=default_allowlist_path())
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="列出已配置 MCP 与 Devin 连接状态")

    for command, aliases, help_text in (
        ("enable", ["connect"], "把一个或多个 MCP 加入白名单"),
        ("disable", ["disconnect"], "从白名单移除一个或多个 MCP"),
        ("only", [], "只允许指定的一个或多个 MCP"),
    ):
        child = subparsers.add_parser(command, aliases=aliases, help=help_text)
        child.add_argument("names", nargs="+")

    subparsers.add_parser("enable-all", help="连接所有已配置 MCP")
    subparsers.add_parser("disable-all", help="禁用所有 MCP")
    subparsers.add_parser("reset", help="恢复默认：只连接 Palace")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    servers = read_configured_mcp_servers(args.root)
    available = list(servers)
    try:
        enabled = read_devin_mcp_allowlist(args.state_file)
        command = args.command
        if command == "list":
            _print_status(servers, enabled, args.state_file)
            return 0
        if command in ("enable", "connect"):
            enabled = _dedupe_names([
                *enabled,
                *_resolve_many(args.names, available),
            ])
        elif command in ("disable", "disconnect"):
            remove = set(_resolve_many(args.names, available))
            enabled = [name for name in enabled if name not in remove]
        elif command == "only":
            enabled = _resolve_many(args.names, available)
        elif command == "enable-all":
            enabled = available
        elif command == "disable-all":
            enabled = []
        elif command == "reset":
            enabled = _resolve_many(["palace"], available)
        else:
            raise DevinMcpConfigError(f"未知命令：{command}")
        enabled = write_devin_mcp_allowlist(args.state_file, enabled)
    except DevinMcpConfigError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    _print_status(servers, enabled, args.state_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
