from __future__ import annotations

from typing import Any


MONITORING_STATES = frozenset({
    "active",
    "idle",
    "blocked_on_user",
    "waiting_on_background",
    "stopped",
})


def require_monitoring_state(value: Any) -> str:
    if not isinstance(value, str) or value not in MONITORING_STATES:
        raise ValueError(f"invalid monitoring state: {value!r}")
    return value
