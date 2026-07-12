"""Auto-restart the backend+frontend via the run.sh supervisor when the
system transitions from busy to idle.

Enabled by the `auto_restart_on_idle` user pref (default OFF). When ON,
active work finishes, the system goes idle, and the repository has advanced
past the running process commit, the monitor fires the same supervisor-restart
path the manual "Refresh" button uses, so code changes are picked up without a
manual reload.

Guarantees:
  - Never fires on the initial idle at boot — only on a real busy→idle
    transition (work must have happened since the last restart).
  - Fires at most once per process — the restart SIGTERMs uvicorn, so a
    second fire within the same process is impossible.
  - Inert unless `BETTER_CLAUDE_RUN_SH_SUPERVISOR=1` — without the outer
    supervisor there is nothing to rebuild and respawn, so restarting
    would just kill the server.

Dependencies (busy check, restart trigger, pref reader) are injected by
main.py at startup. This keeps the module free of main's heavy import
graph so it can be unit-tested in isolation."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Awaitable, Callable

from env_compat import get_env

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 3.0
SUPERVISOR_ENV = "BETTER_CLAUDE_RUN_SH_SUPERVISOR"

BusyCheck = Callable[[], bool]
RestartFn = Callable[[str], Awaitable[None]]
PrefEnabledFn = Callable[[], bool]
NewCommitCheck = Callable[[], bool]


class AutoRestartOnIdleMonitor:
    def __init__(
        self,
        *,
        is_busy: BusyCheck,
        trigger_restart: RestartFn,
        is_enabled: PrefEnabledFn,
        has_new_commit: NewCommitCheck,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._is_busy = is_busy
        self._trigger_restart = trigger_restart
        self._is_enabled = is_enabled
        self._has_new_commit = has_new_commit
        self._poll_interval = poll_interval
        self._was_busy = False
        self._triggered = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception:
                logger.exception("auto-restart-on-idle tick failed")
            await asyncio.sleep(self._poll_interval)

    async def _tick(self) -> None:
        if get_env(SUPERVISOR_ENV) != "1":
            return
        if self._triggered:
            return
        if not await asyncio.to_thread(self._is_enabled):
            # Reset so a later enable-while-idle does not fire on stale history.
            self._was_busy = False
            return
        busy = await asyncio.to_thread(self._is_busy)
        if self._was_busy and not busy:
            if not await asyncio.to_thread(self._has_new_commit):
                self._was_busy = False
                return
            self._triggered = True
            request_id = str(uuid.uuid4())
            logger.info(
                "auto-restart-on-idle: system idle after work, restarting (%s)",
                request_id,
            )
            await self._trigger_restart(request_id)
        self._was_busy = busy
