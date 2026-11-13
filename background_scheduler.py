"""Pure policy shared by paid background schedulers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


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


def choose_devin_background_job(
    *, keepalive_ready: bool, desire_ready: bool, pebbling_due: bool
) -> str | None:
    """Devin gives an eligible desire the slot before KAL and pebbling."""
    if desire_ready:
        return "desire"
    return choose_background_job(
        keepalive_ready=keepalive_ready,
        desire_ready=False,
        pebbling_due=pebbling_due,
    )


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
    """Paid runtimes keep KAL on independently of the stone toggle."""
    del pebbling_enabled
    return runtime in ("openrouter", "devin")


def devin_bedtime_window(
    message: str, now: float, *, duration_seconds: int = 8 * 3600,
) -> tuple[float, float] | None:
    """Return the UTC+8 bedtime window opened by a qualifying 晚安 message."""
    local = datetime.fromtimestamp(now, timezone(timedelta(hours=8)))
    minute = local.hour * 60 + local.minute
    in_trigger_hours = minute >= 22 * 60 + 30 or minute <= 2 * 60
    if "晚安" not in message or not in_trigger_hours:
        return None
    return now, now + duration_seconds


def devin_night_desire_cap_reached(
    *, now: float, window_started_at: float, window_until: float,
    injections: int, limit: int = 2,
) -> bool:
    """Limit Desire only while the explicit eight-hour bedtime window is live."""
    return bool(
        window_started_at
        and window_started_at <= now < window_until
        and injections >= limit
    )
