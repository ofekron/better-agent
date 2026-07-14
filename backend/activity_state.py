from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Optional


FOREGROUND_STATUSES = frozenset({"running", "completed", "failed", "cancelled"})


def transition_activity(
    state: dict[str, Any],
    *,
    foreground_status: Optional[str] = None,
    background_work_ids: Optional[Iterable[str]] = None,
) -> Optional[dict[str, Any]]:
    current_foreground = str(state.get("foreground_status") or "running")
    next_foreground = foreground_status or current_foreground
    if next_foreground not in FOREGROUND_STATUSES:
        raise ValueError(f"invalid foreground status: {next_foreground}")

    current_background = sorted(set(state.get("background_work_ids") or []))
    next_background = sorted(set(
        background_work_ids
        if background_work_ids is not None
        else current_background
    ))
    if (
        next_foreground == current_foreground
        and next_background == current_background
    ):
        return None

    next_state = dict(state)
    next_state["foreground_status"] = next_foreground
    next_state["background_work_ids"] = next_background
    next_state["activity_revision"] = int(state.get("activity_revision") or 0) + 1
    return next_state
