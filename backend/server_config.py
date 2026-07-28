from __future__ import annotations

import os


GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS = 8


def graceful_shutdown_timeout_seconds() -> int:
    value = os.environ.get("BETTER_AGENT_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS")
    if value is None:
        return GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS
    try:
        timeout = int(value)
    except ValueError as exc:
        raise ValueError(
            "BETTER_AGENT_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS must be an integer"
        ) from exc
    if not 1 <= timeout <= 60:
        raise ValueError(
            "BETTER_AGENT_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS must be in 1..60"
        )
    return timeout
