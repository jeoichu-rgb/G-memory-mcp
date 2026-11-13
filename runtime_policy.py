"""Small runtime-routing policies shared by gateway background jobs."""


def subscription_only_task_blocked(
    runtime: str | None,
    api_runtime: str = "openrouter",
) -> bool:
    """Return True when a subscription-only task is bound to an API chat."""
    return runtime == api_runtime


def forge_route(runtime: str, model: str) -> dict[str, str]:
    """Return the only verifier/backend allowed for a source session route."""
    if runtime == "devin":
        verifier = "devin-acp"
    elif runtime in ("subscription", "openrouter"):
        verifier = "claude-tmux"
    else:
        raise ValueError(f"unsupported forge runtime: {runtime}")
    return {"runtime": runtime, "model": model, "verifier": verifier}


def session_route_change_locked(
    current_runtime: str,
    target_runtime: str,
    *,
    backend_bound: bool,
    turn_count: int,
) -> bool:
    """A bound/started session cannot be repointed across paid routes."""
    started = backend_bound or turn_count > 0
    if not started:
        return False
    if current_runtime != target_runtime:
        return True
    return current_runtime in ("openrouter", "devin")
