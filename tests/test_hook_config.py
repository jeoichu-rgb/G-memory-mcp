from hook_config import (
    is_managed_thinking_hook_tool,
    merge_hook_configs,
    strip_managed_thinking_hook,
    with_single_managed_thinking_hook,
)


COMMAND = "python3 /opt/G-memory-mcp/thinking_hook.py"


def test_strip_managed_hook_preserves_other_stop_commands():
    hooks = {
        "Stop": [{
            "matcher": "",
            "hooks": [
                {"type": "command", "command": COMMAND},
                {"type": "command", "command": "python3 keep_me.py"},
            ],
        }],
        "PreToolUse": [{"matcher": "Bash", "hooks": []}],
    }

    cleaned = strip_managed_thinking_hook(hooks)

    assert cleaned["Stop"][0]["hooks"] == [
        {"type": "command", "command": "python3 keep_me.py"}
    ]
    assert cleaned["PreToolUse"] == hooks["PreToolUse"]


def test_single_managed_hook_replaces_duplicates():
    hooks = {
        "Stop": [
            {"matcher": "", "hooks": [{"type": "command", "command": COMMAND}]},
            {"matcher": "", "hooks": [{"type": "command", "command": COMMAND}]},
        ]
    }

    result = with_single_managed_thinking_hook(hooks, COMMAND)

    commands = [
        hook["command"]
        for group in result["Stop"]
        for hook in group["hooks"]
        if hook.get("type") == "command"
    ]
    assert commands == [COMMAND]


def test_merge_hook_configs_deduplicates_equal_groups():
    group = {"matcher": "Bash", "hooks": [{"type": "command", "command": "audit"}]}
    assert merge_hook_configs({"PreToolUse": [group]}, {"PreToolUse": [group]}) == {
        "PreToolUse": [group]
    }


def test_hook_tool_filter_is_narrow():
    assert is_managed_thinking_hook_tool(
        "Ran hook: python3 /opt/G-memory-mcp/thinking_hook.py (stop)"
    )
    assert not is_managed_thinking_hook_tool("Bash")
    assert not is_managed_thinking_hook_tool("Ran hook: python3 another.py (stop)")
