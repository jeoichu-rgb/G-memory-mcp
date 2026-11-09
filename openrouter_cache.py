"""OpenRouter prompt-cache policy shared by the chat gateway and tests.

Claude Code owns the Anthropic Messages request body.  The gateway therefore
uses a tiny local pass-through proxy to add the OpenRouter-only routing fields
without reimplementing Claude Code's agent/tool loop.
"""

from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import os
from typing import Any


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class OpenRouterCacheConfig:
    session_id: str
    ttl: str
    provider_only: tuple[str, ...]
    allow_fallbacks: bool
    proxy_base_url: str
    upstream_base_url: str

    @classmethod
    def from_env(cls) -> "OpenRouterCacheConfig":
        ttl = os.getenv("OPENROUTER_CACHE_TTL", "1h").strip()
        if ttl not in {"5m", "1h"}:
            ttl = "1h"

        session_id = os.getenv(
            "OPENROUTER_SESSION_ID", "jeoi-erik-main"
        ).strip()[:256]
        if not session_id:
            session_id = "jeoi-erik-main"

        providers = tuple(
            item.strip()
            for item in os.getenv("OPENROUTER_PROVIDER_ONLY", "anthropic").split(",")
            if item.strip()
        )
        return cls(
            session_id=session_id,
            ttl=ttl,
            provider_only=providers,
            allow_fallbacks=_env_bool("OPENROUTER_ALLOW_FALLBACKS", True),
            proxy_base_url=os.getenv(
                "OPENROUTER_PROXY_BASE_URL",
                "http://127.0.0.1:3000/openrouter",
            ).rstrip("/"),
            upstream_base_url=os.getenv(
                "OPENROUTER_UPSTREAM_BASE_URL",
                "https://openrouter.ai/api",
            ).rstrip("/"),
        )


def _strip_cache_directives(value: Any) -> None:
    """Remove inherited/automatic breakpoints before placing the exact two."""
    if isinstance(value, dict):
        value.pop("cache_control", None)
        for child in value.values():
            _strip_cache_directives(child)
    elif isinstance(value, list):
        for child in value:
            _strip_cache_directives(child)


def _directive(ttl: str) -> dict[str, str]:
    return {"type": "ephemeral", "ttl": ttl}


def sticky_session_id(logical_session_id: str, namespace: str = "g-memory") -> str:
    """Return one stable, non-identifying OpenRouter route key per chat.

    The logical id is the root gateway session.  A Forge may replace the
    Claude transcript id, but it must keep this route key so follow-up calls
    continue to land on the provider node holding the warm cache.
    """
    logical = (logical_session_id or "main").strip()
    digest = hashlib.sha256(logical.encode("utf-8")).hexdigest()[:32]
    return f"{namespace}:{digest}"


def _set_system_breakpoint(payload: dict[str, Any], ttl: str) -> bool:
    """Put breakpoint 1 at the end of the stable system block."""
    system = payload.get("system")
    if isinstance(system, str):
        if not system.strip():
            return False
        payload["system"] = [{
            "type": "text",
            "text": system,
            "cache_control": _directive(ttl),
        }]
        return True
    if not isinstance(system, list):
        return False
    for block in reversed(system):
        if (isinstance(block, dict)
                and block.get("type", "text") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].strip()):
            block["cache_control"] = _directive(ttl)
            return True
    return False


def _plain_user_text_block(message: Any) -> tuple[str, Any] | None:
    """Return a cacheable text target only for a real, uncomplicated user msg.

    Tool-result continuations and attachment-bearing messages are deliberately
    skipped, matching the rule that complex/unstable turns must not advance the
    progressive cache merely to chase a hit.
    """
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    content = message.get("content")
    if isinstance(content, str):
        return ("string", message) if content.strip() else None
    if not isinstance(content, list) or not content:
        return None
    text_blocks = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            return None
        if isinstance(block.get("text"), str) and block["text"].strip():
            text_blocks.append(block)
    return ("block", text_blocks[-1]) if text_blocks else None


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _set_message_tail_breakpoint(message: Any, ttl: str) -> bool:
    """Put a breakpoint on the last text block of one message."""
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        if not content.strip():
            return False
        message["content"] = [{
            "type": "text",
            "text": content,
            "cache_control": _directive(ttl),
        }]
        return True
    if not isinstance(content, list):
        return False
    for block in reversed(content):
        if (isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].strip()):
            block["cache_control"] = _directive(ttl)
            return True
    return False


_NEW_SESSION_INJECTION_HEADER = (
    "═══ 自动注入 · 以下是系统为你恢复的上下文，不是Jeoi的消息 ═══"
)
_INJECTION_FOOTER = "═══════════════════════════════════════════════════════════════"
_FORGE_VERIFY_MARKER = "[forge-verify]"


def _set_injection_breakpoint(message: Any, ttl: str) -> bool:
    """Split a fresh-session injection from Jeoi's first real message."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return False

    content = message.get("content")
    if isinstance(content, str):
        if (_NEW_SESSION_INJECTION_HEADER not in content
                or _INJECTION_FOOTER not in content):
            return False
        blocks = [{"type": "text", "text": content}]
        message["content"] = blocks
    elif isinstance(content, list):
        blocks = content
    else:
        return False

    for index, block in enumerate(blocks):
        if (not isinstance(block, dict)
                or block.get("type") != "text"
                or not isinstance(block.get("text"), str)):
            continue
        text = block["text"]
        header_at = text.find(_NEW_SESSION_INJECTION_HEADER)
        if header_at < 0:
            continue
        footer_at = text.find(_INJECTION_FOOTER, header_at)
        if footer_at < 0:
            continue
        split_at = footer_at + len(_INJECTION_FOOTER)
        inherited = dict(block)
        inherited["text"] = text[:split_at]
        inherited["cache_control"] = _directive(ttl)
        remainder = text[split_at:]
        replacement = [inherited]
        if remainder:
            current = dict(block)
            current["text"] = remainder
            current.pop("cache_control", None)
            replacement.append(current)
        blocks[index:index + 1] = replacement
        return True
    return False


def _set_inherited_context_breakpoint(
    payload: dict[str, Any], ttl: str
) -> tuple[bool, int]:
    """Fix breakpoint 2 at the end of this Session's inherited prefix.

    A forged Session is delimited by its synthetic verification round. A
    completely fresh Session has no Forge tail; when clip is enabled its
    optional today/yesterday diary and context summary are split from Jeoi's
    first real message and cached on their own.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False, -1

    # The raw marker occurs only on the verifier for the current Forge. Older
    # verifier rounds retained by another Forge are rewritten as 【forge-verify】.
    verify_index = -1
    for index, message in enumerate(messages):
        if (isinstance(message, dict)
                and message.get("role") == "user"
                and _FORGE_VERIFY_MARKER in _message_text(message)):
            verify_index = index
    if verify_index >= 0:
        for index in range(verify_index + 1, len(messages)):
            message = messages[index]
            if not isinstance(message, dict):
                continue
            if message.get("role") == "user":
                break
            if (message.get("role") == "assistant"
                    and _set_message_tail_breakpoint(message, ttl)):
                return True, index
        # During the verification request there is no assistant "ok" yet.
        if _set_message_tail_breakpoint(messages[verify_index], ttl):
            return True, verify_index

    for index in range(len(messages) - 1, -1, -1):
        if _set_injection_breakpoint(messages[index], ttl):
            return True, index
    return False, -1


def _set_progressive_user_breakpoint(
    payload: dict[str, Any], ttl: str, start_at: int = 0
) -> bool:
    """Put breakpoint 3 after the latest completed current-Session turn."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    targets: list[tuple[int, tuple[str, Any]]] = []
    start_at = max(start_at, 0)
    for index in range(start_at, len(messages)):
        message = messages[index]
        target = _plain_user_text_block(message)
        if target:
            targets.append((index, target))
    if len(targets) < 2:
        return False

    # The newest plain user block is the live request. Everything through the
    # last assistant reply before it is completed dialogue and can move into
    # the cache without swallowing the new timestamp/memory/message suffix.
    latest_user_index = targets[-1][0]
    for index in range(latest_user_index - 1, start_at - 1, -1):
        message = messages[index]
        if (isinstance(message, dict)
                and message.get("role") == "assistant"
                and _set_message_tail_breakpoint(message, ttl)):
            return True

    # Consecutive user messages have no completed assistant turn between them.
    # Preserve the old safe fallback rather than dropping progressive caching.
    kind, target = targets[-2][1]
    if kind == "string":
        target["content"] = [{
            "type": "text",
            "text": target["content"],
            "cache_control": _directive(ttl),
        }]
    else:
        target["cache_control"] = _directive(ttl)
    return True


def prepare_openrouter_payload(
    payload: dict[str, Any], config: OpenRouterCacheConfig
) -> tuple[dict[str, Any], int]:
    """Apply stable routing and prompt-cache policy to one inference request.

    The contract is deliberately explicit: one fixed breakpoint at the end of
    system, one fixed breakpoint after inherited Session context when present,
    and one moving breakpoint after the latest completed dialogue turn in the
    current Session. No top-level automatic cache control is used.
    """
    payload["session_id"] = config.session_id

    provider = payload.get("provider")
    if not isinstance(provider, dict):
        provider = {}
        payload["provider"] = provider
    # An explicit order/sort bypasses OpenRouter's cache-aware sticky router.
    # This channel intentionally constrains the candidate set with `only`
    # while leaving endpoint selection to session stickiness.
    provider.pop("order", None)
    provider.pop("sort", None)
    if config.provider_only:
        provider["only"] = list(config.provider_only)
    provider["allow_fallbacks"] = config.allow_fallbacks

    _strip_cache_directives(payload)
    breakpoint_count = int(_set_system_breakpoint(payload, config.ttl))
    inherited_set, inherited_end = _set_inherited_context_breakpoint(
        payload, config.ttl
    )
    breakpoint_count += int(inherited_set)
    breakpoint_count += int(
        _set_progressive_user_breakpoint(
            payload, config.ttl,
            start_at=inherited_end if inherited_set else 0,
        )
    )
    return payload, breakpoint_count


def _has_cache_control(value: Any) -> bool:
    if isinstance(value, dict):
        if isinstance(value.get("cache_control"), dict):
            return True
        return any(_has_cache_control(child) for child in value.values())
    if isinstance(value, list):
        return any(_has_cache_control(child) for child in value)
    return False


def apply_output_token_cap(
    payload: dict[str, Any], limit: int,
) -> tuple[bool, str, int | None]:
    """Apply a small output ceiling without creating invalid thinking input.

    Manual extended thinking requires ``budget_tokens < max_tokens``.  Those
    legacy requests keep their original ceiling instead of turning a cheap
    background call into an HTTP 400.  Adaptive thinking has no fixed budget
    and can safely share the requested hard ceiling.
    """
    if not isinstance(payload, dict) or limit <= 0:
        return False, "invalid_limit", None

    thinking = payload.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        budget = thinking.get("budget_tokens")
        if isinstance(budget, int) and not isinstance(budget, bool):
            if budget >= limit:
                current = payload.get("max_tokens")
                effective = current if isinstance(current, int) else None
                return False, "manual_thinking_budget", effective

    keys = [
        key for key in ("max_tokens", "max_completion_tokens")
        if key in payload
    ]
    if not keys:
        keys = ["max_tokens"]

    effective = limit
    for key in keys:
        current = payload.get(key)
        if (isinstance(current, int) and not isinstance(current, bool)
                and current > 0):
            value = min(current, limit)
        else:
            value = limit
        payload[key] = value
        effective = min(effective, value)
    return True, "applied", effective


def build_keepalive_payload(
    prepared_payload: dict[str, Any],
    prompt: str = (
        "[cache-keepalive] This is not a user message. Do not use tools. "
        "Reply with a single period."
    ),
) -> dict[str, Any] | None:
    """Build a cache-refresh request without mutating conversation history.

    Only the prefix through the last explicit cache breakpoint is replayed.
    A tiny uncached suffix is then added so the provider performs a normal,
    valid Messages request.  Existing model, tools, thinking/effort and token
    parameters are preserved deliberately: changing them can put a heartbeat
    in a different cache universe.  In particular this function never writes
    ``max_tokens=0`` or ``max_completion_tokens=0``.
    """
    if not isinstance(prepared_payload, dict):
        return None

    result = copy.deepcopy(prepared_payload)
    messages = result.get("messages")
    if not isinstance(messages, list):
        messages = []

    last_cached_index = -1
    for index, message in enumerate(messages):
        if _has_cache_control(message):
            last_cached_index = index

    system_cached = _has_cache_control(result.get("system"))
    if last_cached_index < 0 and not system_cached:
        return None

    kept = copy.deepcopy(messages[:last_cached_index + 1])
    keepalive_block = {"type": "text", "text": prompt}

    if kept and isinstance(kept[-1], dict) and kept[-1].get("role") == "user":
        # Keep consecutive user content inside the same message.  The new text
        # sits after the cached block, so it does not alter the cached prefix.
        content = kept[-1].get("content")
        if isinstance(content, str):
            kept[-1]["content"] = [
                {"type": "text", "text": content}, keepalive_block,
            ]
        elif isinstance(content, list):
            content.append(keepalive_block)
        else:
            kept[-1]["content"] = [keepalive_block]
    else:
        continuation: list[dict[str, Any]] = []
        # If the cached assistant tail contains tool_use blocks, close them
        # with synthetic results before adding text.  This keeps the request
        # valid while leaving every cached byte untouched.
        if kept and isinstance(kept[-1], dict):
            content = kept[-1].get("content")
            if isinstance(content, list):
                for block in content:
                    if (isinstance(block, dict)
                            and block.get("type") == "tool_use"
                            and block.get("id")):
                        continuation.append({
                            "type": "tool_result",
                            "tool_use_id": block["id"],
                            "content": "Not executed during cache keepalive.",
                            "is_error": True,
                        })
        continuation.append(keepalive_block)
        kept.append({"role": "user", "content": continuation})

    result["messages"] = kept
    result["stream"] = False
    return result


def merge_custom_headers(existing: str, session_id: str,
                         proxy_secret: str = "") -> str:
    """Preserve configured Claude headers while forcing one sticky route key."""
    pairs: list[tuple[str, str]] = []
    seen_session = False
    seen_proxy_secret = False
    for line in (existing or "").splitlines():
        name, sep, value = line.partition(":")
        if not sep or not name.strip():
            continue
        name = name.strip()
        if name.lower() == "x-session-id":
            value = session_id
            seen_session = True
        elif name.lower() == "x-g-memory-proxy-secret":
            if not proxy_secret:
                continue
            value = proxy_secret
            seen_proxy_secret = True
        pairs.append((name, value.strip()))
    if not seen_session:
        pairs.append(("x-session-id", session_id))
    if proxy_secret and not seen_proxy_secret:
        pairs.append(("x-g-memory-proxy-secret", proxy_secret))
    return "\n".join(f"{name}: {value}" for name, value in pairs)
