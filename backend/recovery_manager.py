"""Execution host for crash recovery.

Recovery is startup-time work that competes with request handlers for
both the main event loop and asyncio's default executor. This module
gives it a home of its own: one dedicated thread running one event
loop, backed by a bounded executor, so a long run-directory scan or
replay cannot starve a user's first request. The thread/loop/executor
machinery itself lives in `thread_loop_host`.

The manager is an execution host, not a policy owner. It knows how to
run work off the main loop and how to announce what happened; it does
not decide what recovery means. Phases move onto it one at a time, and
only after each phase is shown to be free of main-loop affinity —
`recovery_schedule` owns bucket ordering, `run_recovery` still owns the
integration pipeline.

Announcements go out as facts on the event bus
(`recovery.started` / `recovery.session_ready` / `recovery.finished` /
`recovery.failed`), published cross-thread so main-loop subscribers
receive them on the main loop. Facts describe what happened; the
subscriber decides what to do about it.
"""

from __future__ import annotations

from thread_loop_host import ThreadLoopHost

RECOVERY_STARTED = "recovery.started"
RECOVERY_SESSION_READY = "recovery.session_ready"
RECOVERY_FINISHED = "recovery.finished"
RECOVERY_FAILED = "recovery.failed"

# Blocking recovery IO (run-dir scans, replay reads, marker writes) runs
# on this host's executor instead of asyncio's default one, so it cannot
# exhaust the thread pool that live request handlers share.
_EXECUTOR_WORKERS = 4


class RecoveryManager(ThreadLoopHost):
    """Owns the recovery thread, its loop, and its executor."""

    def __init__(self, *, executor_workers: int = _EXECUTOR_WORKERS) -> None:
        super().__init__(
            name="recovery-manager",
            executor_workers=executor_workers,
            io_thread_name_prefix="recovery-io",
        )


manager = RecoveryManager()
