"""Orchestration mode packages.

Each subpackage implements one orchestration mode (manager, native). At
minimum a mode package exposes:

  - `handle_turn(coordinator, app_session_id, prompt, ...)` async fn
    that runs one user-prompt → assistant-reply turn for that mode.

The dispatch table lives in `MODE_HANDLERS` below; `Coordinator.handle_prompt`
looks the active mode up here rather than branching on string equality.

`orchs.supervisor` is NOT a mode — it's a per-session toggle layered
on top of native/manager (see `session["supervisor_enabled"]`). The
package exports `request_verdict`, `request_review`, and
`maybe_run_verdict_loop` for use by the primary handlers.
"""

from typing import Awaitable, Callable

from orchs.base import ApplyEventCtx, OrchestrationStrategy

# Lazy import inside the dispatch fn so a mode package can safely
# import from `orchestrator` (the coordinator class) without a cycle.

ModeHandler = Callable[..., Awaitable[None]]

_strategy: OrchestrationStrategy | None = None


def get_strategy(mode: str) -> OrchestrationStrategy:
    """Resolve the single orchestration strategy.

    `mode` is accepted for call-site compatibility but no longer selects
    state shape — there is one strategy for all modes. Mode-specific
    behavior (bootstrap prompt) lives in `wrap_cli_prompt`, which reads
    `session["orchestration_mode"]` directly.
    """
    if mode == "manager":
        mode = "team"
    if mode not in ("team", "native"):
        raise ValueError(f"unknown orchestration_mode: {mode!r}")
    global _strategy
    if _strategy is None:
        from orchs.native import NativeStrategy
        _strategy = NativeStrategy()
    return _strategy


def get_handler(mode: str) -> ModeHandler:
    """Resolve the single turn handler.

    Raises ValueError on unknown mode rather than silently falling
    back — an unknown mode is always a bug.
    """
    if mode == "manager":
        mode = "team"
    if mode not in ("team", "native"):
        raise ValueError(f"unknown orchestration_mode: {mode!r}")
    from orchs.native import handle_turn as _h
    return _h
