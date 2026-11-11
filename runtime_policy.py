"""Small runtime-routing policies shared by gateway background jobs."""


def subscription_only_task_blocked(
    runtime: str | None,
    api_runtime: str = "openrouter",
) -> bool:
    """Return True when a subscription-only task is bound to an API chat."""
    return runtime == api_runtime
