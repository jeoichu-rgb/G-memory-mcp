"""Pure policy shared by paid background schedulers."""

from __future__ import annotations


def choose_background_job(
    *, keepalive_ready: bool, desire_ready: bool, pebbling_due: bool
) -> str | None:
    """Preserve the strict KAL > desire > pebbling arbitration order."""
    if keepalive_ready:
        return "keepalive"
    if desire_ready:
        return "desire"
    if pebbling_due:
        return "pebbling"
    return None


def scheduler_slot_due(
    *, active: bool, bound: bool, last_request_at: float,
    now: float, interval_seconds: int,
) -> bool:
    """Return whether the shared inactivity slot may run."""
    return bool(
        active
        and bound
        and last_request_at
        and now - last_request_at >= interval_seconds
    )


def keepalive_enabled_for_runtime(
    runtime: str, pebbling_enabled: bool,
) -> bool:
    """OpenRouter KAL is always on; Devin KAL follows the stone toggle."""
    if runtime == "openrouter":
        return True
    if runtime == "devin":
        return pebbling_enabled
    return False
