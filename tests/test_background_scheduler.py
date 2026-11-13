from datetime import datetime, timedelta, timezone

from background_scheduler import (
    choose_background_job,
    choose_devin_background_job,
    devin_bedtime_window,
    devin_night_desire_cap_reached,
    keepalive_enabled_for_runtime,
    scheduler_slot_due,
)


def test_priority_is_keepalive_then_desire_then_pebbling():
    assert choose_background_job(
        keepalive_ready=True, desire_ready=True, pebbling_due=True
    ) == "keepalive"
    assert choose_background_job(
        keepalive_ready=False, desire_ready=True, pebbling_due=True
    ) == "desire"
    assert choose_background_job(
        keepalive_ready=False, desire_ready=False, pebbling_due=True
    ) == "pebbling"
    assert choose_background_job(
        keepalive_ready=False, desire_ready=False, pebbling_due=False
    ) is None


def test_devin_priority_is_desire_then_keepalive_then_pebbling():
    assert choose_devin_background_job(
        keepalive_ready=True, desire_ready=True, pebbling_due=True
    ) == "desire"
    assert choose_devin_background_job(
        keepalive_ready=True, desire_ready=False, pebbling_due=True
    ) == "keepalive"
    assert choose_devin_background_job(
        keepalive_ready=False, desire_ready=False, pebbling_due=True
    ) == "pebbling"


def test_slot_requires_bound_active_session_and_full_interval():
    base = dict(
        active=True,
        bound=True,
        last_request_at=100.0,
        interval_seconds=3000,
    )
    assert not scheduler_slot_due(**base, now=3099.9)
    assert scheduler_slot_due(**base, now=3100.0)
    assert not scheduler_slot_due(**{**base, "active": False}, now=4000)
    assert not scheduler_slot_due(**{**base, "bound": False}, now=4000)
    assert not scheduler_slot_due(**{**base, "last_request_at": 0}, now=4000)


def test_paid_kal_is_always_on_independent_of_stone_toggle():
    assert keepalive_enabled_for_runtime("openrouter", False)
    assert keepalive_enabled_for_runtime("openrouter", True)
    assert keepalive_enabled_for_runtime("devin", False)
    assert keepalive_enabled_for_runtime("devin", True)
    assert not keepalive_enabled_for_runtime("subscription", True)


def _utc8_timestamp(hour: int, minute: int) -> float:
    tz = timezone(timedelta(hours=8))
    return datetime(2026, 11, 13, hour, minute, tzinfo=tz).timestamp()


def test_devin_bedtime_window_requires_goodnight_during_trigger_hours():
    assert devin_bedtime_window("晚安", _utc8_timestamp(22, 29)) is None
    assert devin_bedtime_window("晚安", _utc8_timestamp(2, 1)) is None
    assert devin_bedtime_window("睡了", _utc8_timestamp(23, 0)) is None

    start = _utc8_timestamp(22, 30)
    assert devin_bedtime_window("晚安呀", start) == (start, start + 8 * 3600)
    assert devin_bedtime_window("晚安", _utc8_timestamp(2, 0)) is not None


def test_devin_desire_cap_only_applies_inside_bedtime_window():
    start = _utc8_timestamp(23, 0)
    end = start + 8 * 3600
    assert not devin_night_desire_cap_reached(
        now=start + 100, window_started_at=start, window_until=end,
        injections=1,
    )
    assert devin_night_desire_cap_reached(
        now=start + 100, window_started_at=start, window_until=end,
        injections=2,
    )
    assert not devin_night_desire_cap_reached(
        now=end, window_started_at=start, window_until=end, injections=2,
    )
