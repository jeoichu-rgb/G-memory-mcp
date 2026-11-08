"""OpenRouter prompt-cache policy shared by the chat gateway and tests.

Claude Code owns the Anthropic Messages request body.  The gateway therefore
uses a tiny local pass-through proxy to add the OpenRouter-only routing fields
without reimplementing Claude Code's agent/tool loop.
"""

from __future__ import annotations

from dataclasses import dataclass
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


def _set_progressive_user_breakpoint(payload: dict[str, Any], ttl: str) -> bool:
    """Put breakpoint 2 on the second-to-last real user message."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    targets = []
    for message in messages:
        target = _plain_user_text_block(message)
        if target:
            targets.append(target)
    if len(targets) < 2:
        return False
    kind, target = targets[-2]
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
    system, and one moving breakpoint on the second-to-last plain user
    message. No top-level automatic cache control is used.
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
    breakpoint_count += int(
        _set_progressive_user_breakpoint(payload, config.ttl)
    )
    return payload, breakpoint_count


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

