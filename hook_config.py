"""Helpers for managing the gateway-owned Claude Code Stop hook."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


MANAGED_HOOK_SCRIPT = "thinking_hook.py"


def is_managed_thinking_hook_tool(name: Any) -> bool:
    """Return whether a transcript tool block is our Stop-hook status line."""
    normalized = str(name or "").strip().lower()
    return (
        "ran hook:" in normalized
        and MANAGED_HOOK_SCRIPT in normalized
        and "(stop)" in normalized
    )


def _is_managed_hook_command(hook: Any) -> bool:
    return (
        isinstance(hook, dict)
        and hook.get("type") == "command"
        and MANAGED_HOOK_SCRIPT in str(hook.get("command", ""))
    )


def strip_managed_thinking_hook(hooks: Any) -> dict:
    """Remove only this gateway's command from a Claude hooks mapping.

    Other event types, Stop groups, matchers and commands are preserved. Empty
    Stop groups created by removing the managed command are discarded.
    """
    if not isinstance(hooks, dict):
        return {}

    cleaned = deepcopy(hooks)
    stop_groups = cleaned.get("Stop")
    if not isinstance(stop_groups, list):
        return cleaned

    kept_groups = []
    for group in stop_groups:
        if not isinstance(group, dict):
            kept_groups.append(group)
            continue
        commands = group.get("hooks")
        if not isinstance(commands, list):
            kept_groups.append(group)
            continue
        kept_commands = [item for item in commands
                         if not _is_managed_hook_command(item)]
        if kept_commands:
            kept_group = deepcopy(group)
            kept_group["hooks"] = kept_commands
            kept_groups.append(kept_group)

    if kept_groups:
        cleaned["Stop"] = kept_groups
    else:
        cleaned.pop("Stop", None)
    return cleaned


def merge_hook_configs(*configs: Any) -> dict:
    """Merge hook mappings while de-duplicating structurally equal groups."""
    merged: dict[str, Any] = {}
    for config in configs:
        if not isinstance(config, dict):
            continue
        for event, groups in config.items():
            if not isinstance(groups, list):
                if event not in merged:
                    merged[event] = deepcopy(groups)
                continue
            target = merged.setdefault(event, [])
            if not isinstance(target, list):
                continue
            for group in groups:
                if group not in target:
                    target.append(deepcopy(group))
    return merged


def with_single_managed_thinking_hook(hooks: Any, command: str) -> dict:
    """Return hooks with exactly one gateway-managed Stop command."""
    cleaned = strip_managed_thinking_hook(hooks)
    stop_groups = cleaned.setdefault("Stop", [])
    if not isinstance(stop_groups, list):
        stop_groups = []
        cleaned["Stop"] = stop_groups
    stop_groups.append({
        "matcher": "",
        "hooks": [{"type": "command", "command": command}],
    })
    return cleaned
