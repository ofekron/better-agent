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
from stores import task_trigger_store
import task_script

logger = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 10.0


async def _noop_ws_callback(_event: dict) -> None:
    # submit_prompt requires a ws_callback in params; the per-session
    # processor replaces it with the registry-based dispatcher before
    # handle_prompt runs.
    return None


async def broadcast_schedules(coordinator, app_session_id: str) -> None:
    """Push the session's full schedule list to every connected tab,
    plus a global cross-session invalidation ping (the Schedules page
    refetches its snapshot on `schedules_changed`)."""
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
    try:
        await coordinator.broadcast_global("schedules_changed", {})
    except Exception:
        logger.debug("schedules_changed broadcast failed", exc_info=True)


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
                await self.fire_task_triggers()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduler tick failed")
            await asyncio.sleep(self._tick_interval)

    async def fire_due(self, now: datetime | None = None) -> int:
        """Fire every due schedule. Returns the number fired (exposed
        for tests)."""
        fired = 0
        # due() does a synchronous stat + read_text + json.loads under the
        # store lock; under disk contention that stalled the main event loop
        # for seconds (lag-watchdog pinned at schedule_store._read stat).
        # Read off-loop so the loop stays responsive; the firing loop below
        # stays on-loop (submit_prompt is the serialized per-session funnel).
        recs = await asyncio.to_thread(schedule_store.due, now)
        for rec in recs:
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
            try:
                import config_store
                provider_id = session.get("provider_id") or config_store.default_session_provider_id()
                if provider_id and config_store.provider_suspended(provider_id):
                    logger.info(
                        "scheduler: delaying schedule %s for session %s — provider %s suspended",
                        rec["id"], sid, provider_id,
                    )
                    await broadcast_schedules(self._coordinator, sid)
                    continue
            except Exception:
                logger.debug("scheduler: provider suspension check failed", exc_info=True)
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
                            source_task_id=rec.get("source_task_id"),
                        )
                    except ValueError:
                        logger.exception(
                            "scheduler: could not restore once schedule %s",
                            rec["id"],
                        )
            await broadcast_schedules(self._coordinator, sid)
        return fired

    async def fire_task_triggers(self, now: datetime | None = None) -> int:
        """Fire due task triggers (schedule + script-detector). Returns the
        number of task launches triggered (exposed for tests). Script detectors
        only launch on exit 0 but always advance their poll window so a failing
        detector backs off instead of hot-spinning."""
        import task_runner

        now = now or datetime.now()
        launched = 0
        # Off-loop for the same reason as fire_due: due() stats+reads the
        # trigger store synchronously and stalled the loop under disk
        # contention (lag-watchdog pinned at task_trigger_store._fingerprint
        # stat, 5.3s).
        recs = await asyncio.to_thread(task_trigger_store.due, now)
        for rec in recs:
            trigger_id = rec["id"]
            task_id = rec.get("task_id")
            if not task_id:
                await asyncio.to_thread(task_trigger_store.mark_fired, trigger_id, now)
                continue
            is_turn_end = rec.get("kind") == "turn_end_once"
            if is_turn_end:
                is_current, _task = await asyncio.to_thread(
                    task_trigger_store.receipt_task_snapshot, trigger_id,
                )
                if not is_current:
                    await asyncio.to_thread(task_trigger_store.mark_fired, trigger_id, now)
                    continue
            if rec.get("kind") == "script":
                detector = rec.get("detector")
                res = await asyncio.to_thread(
                    task_script.run_script, detector, timeout=30,
                )
                # Advance the poll window either way (at-most-once + backoff).
                await asyncio.to_thread(task_trigger_store.mark_fired, trigger_id, now)
                if res is None or not res.ok:
                    logger.info(
                        "scheduler: task %s detector did not fire (exit %s)",
                        task_id, res.exit_code if res else "n/a",
                    )
                    continue
            elif not is_turn_end:
                await asyncio.to_thread(task_trigger_store.mark_fired, trigger_id, now)
            try:
                client_id = None
                source = "trigger"
                if is_turn_end:
                    client_id = f"routine-event:{trigger_id}"
                    source = "turn_end_trigger"
                await task_runner.launch_task(
                    task_id,
                    coordinator=self._coordinator,
                    client_id=client_id,
                    source=source,
                    event_receipt_id=trigger_id if is_turn_end else None,
                )
                if is_turn_end:
                    await asyncio.to_thread(task_trigger_store.mark_fired, trigger_id, now)
                launched += 1
                logger.info(
                    "scheduler: trigger %s launched task %s",
                    trigger_id, task_id,
                )
            except Exception:
                logger.exception(
                    "scheduler: launch failed for trigger %s task %s",
                    trigger_id, task_id,
                )
                if is_turn_end:
                    is_current, _task = await asyncio.to_thread(
                        task_trigger_store.receipt_task_snapshot, trigger_id,
                    )
                    if is_current:
                        await asyncio.to_thread(task_trigger_store.retry_later, trigger_id, now)
                    else:
                        await asyncio.to_thread(task_trigger_store.mark_fired, trigger_id, now)
            await task_runner.broadcast_tasks_changed(
                self._coordinator,
                rec.get("task_cwd") or "",
                rec.get("task_node_id") or "primary",
            )
        return launched
