"""Proves the bucket routing actually dispatches, not just that the
predicate returns the right answer.

Drives the real `integrate_recovered_runs` with a mix of replay-only and
reattach-bound buckets and records which thread each bucket's group
integration ran on. Startup recovery cannot be exercised end to end here
(a fresh BETTER_AGENT_HOME has no installation profile, so the startup
task returns early), so this drives the integration entry point itself.
"""

import asyncio
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths

paths.engage_test_home(tempfile.mkdtemp(prefix="routing-live-"))

import recovery_manager  # noqa: E402
import recovery_schedule  # noqa: E402
import run_recovery  # noqa: E402


def _desc(run_id, sid, started, **kw):
    d = {"run_id": run_id, "persist_to": sid, "started_at": started}
    d.update(kw)
    return d


def _drive(descs):
    """Run integrate_recovered_runs, returning {session_key: thread_name}."""
    seen: dict[str, str] = {}
    original_group = run_recovery._integrate_recovered_session_group
    original_cap = run_recovery._is_provider_capability_change

    async def spy(coordinator, group_descs, summary):
        key = run_recovery._session_key(group_descs[0])
        seen[key] = threading.current_thread().name

    run_recovery._integrate_recovered_session_group = spy
    run_recovery._is_provider_capability_change = lambda run_id: run_id.startswith(
        "cap-",
    )
    recovery_manager.manager.start()
    try:
        asyncio.run(run_recovery.integrate_recovered_runs(None, descs))
    finally:
        recovery_manager.manager.stop()
        recovery_schedule.set_active(None)
        run_recovery._integrate_recovered_session_group = original_group
        run_recovery._is_provider_capability_change = original_cap
    return seen


def test_replay_only_bucket_runs_on_the_recovery_thread() -> None:
    seen = _drive([
        _desc("r1", "sid-dead", "2026-07-29T00:00:00Z",
              alive=False, has_complete_json=True),
    ])
    assert seen["sid-dead"] == "recovery-manager", seen


def test_live_bucket_stays_on_the_calling_thread() -> None:
    seen = _drive([
        _desc("r2", "sid-live", "2026-07-29T00:00:00Z",
              alive=True, has_complete_json=False),
    ])
    assert seen["sid-live"] == "MainThread", seen


def test_capability_change_bucket_stays_on_the_calling_thread() -> None:
    """Completed payload, but retried as a fresh live run — must not
    leave the main loop."""
    seen = _drive([
        _desc("cap-1", "sid-cap", "2026-07-29T00:00:00Z",
              alive=False, has_complete_json=True),
    ])
    assert seen["sid-cap"] == "MainThread", seen


def test_mixed_batch_splits_across_both_threads() -> None:
    seen = _drive([
        _desc("d1", "sid-a", "2026-07-29T00:00:00Z",
              alive=False, has_complete_json=True),
        _desc("d2", "sid-b", "2026-07-28T00:00:00Z",
              alive=False, has_complete_json=True),
        _desc("l1", "sid-c", "2026-07-27T00:00:00Z",
              alive=True, has_complete_json=False),
        _desc("cap-2", "sid-d", "2026-07-26T00:00:00Z",
              alive=False, has_complete_json=True),
    ])
    assert seen["sid-a"] == "recovery-manager", seen
    assert seen["sid-b"] == "recovery-manager", seen
    assert seen["sid-c"] == "MainThread", seen
    assert seen["sid-d"] == "MainThread", seen


def test_every_bucket_is_integrated_exactly_once() -> None:
    descs = [
        _desc(f"d{i}", f"sid-{i}", f"2026-07-{10 + i:02d}T00:00:00Z",
              alive=(i % 2 == 0), has_complete_json=(i % 2 == 1))
        for i in range(6)
    ]
    seen = _drive(descs)
    assert len(seen) == 6, seen


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("PASS")
