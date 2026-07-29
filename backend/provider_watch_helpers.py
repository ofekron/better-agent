from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from provider import StreamEvent, path_exists_off_loop, popen_is_running_off_loop


async def wait_for_complete_or_process_death(
    *,
    complete_path: Path,
    popen: Any,
    poll_interval: float,
) -> None:
    """Block until complete.json appears, or the process has died and a
    short grace window (poll_interval * 6) for a late-arriving
    complete.json has elapsed."""
    while True:
        if await path_exists_off_loop(complete_path):
            return
        if not await popen_is_running_off_loop(popen):
            loop = asyncio.get_event_loop()
            grace_end = loop.time() + (poll_interval * 6)
            while (
                not await path_exists_off_loop(complete_path)
                and loop.time() < grace_end
            ):
                await asyncio.sleep(poll_interval)
            return
        await asyncio.sleep(poll_interval)


async def emit_early_failure(
    *,
    logger: logging.Logger,
    log_prefix: str,
    run_id: str,
    msg: str,
    queue: Any,
    cleanup: Callable[[], None],
    before_enqueue: Optional[Callable[[], None]] = None,
) -> None:
    """Synthesize error+complete events for a bootstrap failure that happened
    before the tailer/watch tasks ever started, then clean up the run."""
    logger.warning("%s bootstrap failure for %s: %s", log_prefix, run_id, msg)
    if before_enqueue is not None:
        before_enqueue()
    try:
        queue.put_nowait(StreamEvent("error", {"error": msg}))
        queue.put_nowait(StreamEvent("complete", {
            "success": False, "error": msg,
            "session_id": None, "token_usage": None,
        }))
    except Exception:
        logger.exception("failed to enqueue early failure for %s", run_id)
    cleanup()
