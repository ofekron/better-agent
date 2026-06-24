"""Backend-owned schedule ticker.

Owns the firing side of `stores/schedule_store.py`: a single global
asyncio loop scans for due schedules and submits each as a normal
prompt through `coordinator.submit_prompt` — the same serialized
per-session funnel user prompts use, so a scheduled turn queues behind
an in-flight turn and inherits the full ingestion/convergence path.

Overdue schedules at backend startup fire once on the first tick
(recurring ones advance past `now`), which is the catch-up semantic:
a schedule missed while the backend was down fires once, not N times.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from session_manager import manager as session_manager
from stores import schedule_store

logger = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 10.0


async def _noop_ws_callback(_event: dict) -> None:
    # submit_prompt requires a ws_callback in params; the per-session
    # processor replaces it with the registry-based dispatcher before
    # handle_prompt runs.
    return None


async def broadcast_schedules(coordinator, app_session_id: str) -> None:
    """Push the session's full schedule list to every connected tab."""
    try:
        await coordinator.dispatch_raw(app_session_id, {
            "type": "schedules_updated",
            "data": {
                "app_session_id": app_session_id,
                "schedules": schedule_store.list_for_session(app_session_id),
            },
        })
    except Exception:
        logger.debug("schedules_updated broadcast failed", exc_info=True)


class Scheduler:
    """Single global ticker; one instance per backend process."""

    def __init__(self, coordinator, tick_interval: float = TICK_INTERVAL_SECONDS):
        self._coordinator = coordinator
        self._tick_interval = tick_interval
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="schedule-ticker")
        logger.info("scheduler: ticker started")

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass

    async def _loop(self) -> None:
        while True:
            try:
                await self.fire_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduler tick failed")
            await asyncio.sleep(self._tick_interval)

    async def fire_due(self, now: datetime | None = None) -> int:
        """Fire every due schedule. Returns the number fired (exposed
        for tests)."""
        fired = 0
        for rec in schedule_store.due(now):
            sid = rec["app_session_id"]
            session = session_manager.get(sid)
            if session is None:
                logger.info(
                    "scheduler: dropping schedule %s — session %s gone",
                    rec["id"], sid,
                )
                schedule_store.delete(rec["id"])
                await broadcast_schedules(self._coordinator, sid)
                continue
            # Mark BEFORE submit: at-most-once on crash (see store docs).
            schedule_store.mark_fired(rec["id"], now)
            params = {
                "prompt": rec["prompt"],
                "app_session_id": sid,
                # Session record is authoritative for model/cwd; pass its
                # values so handle_prompt's stored-wins checks are no-ops.
                "model": session.get("model"),
                "cwd": session.get("cwd"),
                "ws_callback": _noop_ws_callback,
                "source": "schedule",
                "user_initiated": False,
            }
            try:
                self._coordinator.submit_prompt(sid, params)
                fired += 1
                logger.info(
                    "scheduler: fired schedule %s into session %s",
                    rec["id"], sid,
                )
            except Exception:
                logger.exception(
                    "scheduler: submit failed for schedule %s session %s",
                    rec["id"], sid,
                )
                if rec.get("kind") == "once":
                    # mark_fired already deleted it (mark-before-submit);
                    # restore with a 5-minute backoff so a transiently
                    # locked queue (e.g. adv_sync overlay) doesn't
                    # silently drop the prompt.
                    try:
                        schedule_store.create(
                            app_session_id=sid,
                            prompt=rec["prompt"],
                            kind="once",
                            fire_at=(
                                datetime.now() + timedelta(minutes=5)
                            ).isoformat(),
                        )
                    except ValueError:
                        logger.exception(
                            "scheduler: could not restore once schedule %s",
                            rec["id"],
                        )
            await broadcast_schedules(self._coordinator, sid)
        return fired
