"""Cross-process cooldown/backoff guard for `auto_restart_on_idle`.

`AutoRestartOnIdleMonitor` (auto_restart_on_idle.py) fires at most once per
*process* — the restart SIGTERMs uvicorn, so a second fire within the same
process is structurally impossible. But the restart respawns a brand-new
process (via run.sh) that rebuilds the monitor from scratch with no memory
of when the previous process fired. If the busy signal keeps flapping
idle<->busy across respawns (e.g. during heavy startup-recovery lock
contention), each freshly booted process can independently decide "busy then
idle, and a newer commit exists" and fire again within seconds of booting —
an unbounded restart storm.

This module persists the last-fired timestamp (and a fast-repeat counter)
to disk under `ba_home()` so a fresh process can see how recently *any*
process last auto-restarted and back off. Cooldown grows exponentially with
consecutive fast repeats and resets once a fired restart is followed by a
long enough gap before the next one, so a single legitimate restart under
normal conditions never gets throttled."""

from __future__ import annotations

import time

from json_store import read_json, write_json_durable
from paths import ba_home

STATE_FILENAME = "auto_restart_on_idle_state.json"

BASE_COOLDOWN_SECONDS = 300.0
MAX_COOLDOWN_SECONDS = 3600.0
BACKOFF_RESET_SECONDS = 1800.0

_DEFAULT_STATE = {"last_fired_at": None, "consecutive_fast_restarts": 0}


def _state_path():
    return ba_home() / STATE_FILENAME


def _read_state() -> dict:
    return read_json(_state_path(), dict(_DEFAULT_STATE))


def _required_cooldown(consecutive_fast_restarts: int) -> float:
    return min(
        BASE_COOLDOWN_SECONDS * (2 ** max(0, consecutive_fast_restarts)),
        MAX_COOLDOWN_SECONDS,
    )


def restart_cooldown_remaining_seconds() -> float:
    """Seconds until another auto-restart is permitted; 0 if allowed now."""
    state = _read_state()
    last_fired_at = state.get("last_fired_at")
    if last_fired_at is None:
        return 0.0
    consecutive = int(state.get("consecutive_fast_restarts", 0) or 0)
    cooldown = _required_cooldown(consecutive)
    remaining = cooldown - (time.time() - float(last_fired_at))
    return max(0.0, remaining)


def record_restart_fired() -> None:
    """Persist that an auto-restart is firing now, updating the fast-repeat
    backoff counter for the next process to read."""
    now = time.time()
    state = _read_state()
    last_fired_at = state.get("last_fired_at")
    if last_fired_at is not None and (now - float(last_fired_at)) < BACKOFF_RESET_SECONDS:
        consecutive = int(state.get("consecutive_fast_restarts", 0) or 0) + 1
    else:
        consecutive = 0
    write_json_durable(
        _state_path(),
        {"last_fired_at": now, "consecutive_fast_restarts": consecutive},
    )
