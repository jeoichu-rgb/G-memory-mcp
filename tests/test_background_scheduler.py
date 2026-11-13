from background_scheduler import (
    choose_background_job,
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


def test_openrouter_kal_is_always_on_but_devin_follows_stone_toggle():
    assert keepalive_enabled_for_runtime("openrouter", False)
    assert keepalive_enabled_for_runtime("openrouter", True)
    assert not keepalive_enabled_for_runtime("devin", False)
    assert keepalive_enabled_for_runtime("devin", True)
    assert not keepalive_enabled_for_runtime("subscription", True)
