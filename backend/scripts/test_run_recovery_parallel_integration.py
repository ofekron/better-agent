"""Regression tests for bounded-parallel recovered-run integration.

Run with:
    cd backend && python3 scripts/test_run_recovery_parallel_integration.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home  # noqa: E402
_test_home.isolate("bc-test-recovery-parallel-")

import run_recovery  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


async def _test_independent_session_buckets_run_in_parallel() -> bool:
    original = run_recovery._integrate_recovered_session_group
    seen: list[list[str]] = []
    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def fake_group(_coordinator, descs, _summary):
        nonlocal active, max_active
        async with lock:
            active += 1
            max_active = max(max_active, active)
        seen.append([str(d["run_id"]) for d in descs])
        await asyncio.sleep(0.15)
        async with lock:
            active -= 1

    old_env = os.environ.get(run_recovery._RECOVERY_INTEGRATION_PARALLELISM_ENV)
    os.environ[run_recovery._RECOVERY_INTEGRATION_PARALLELISM_ENV] = "4"
    run_recovery._integrate_recovered_session_group = fake_group
    try:
        recovered = [
            {"run_id": f"run-{i}", "app_session_id": f"session-{i}"}
            for i in range(4)
        ]
        started = time.monotonic()
        await run_recovery.integrate_recovered_runs(None, recovered)
        elapsed = time.monotonic() - started
    finally:
        run_recovery._integrate_recovered_session_group = original
        if old_env is None:
            os.environ.pop(run_recovery._RECOVERY_INTEGRATION_PARALLELISM_ENV, None)
        else:
            os.environ[run_recovery._RECOVERY_INTEGRATION_PARALLELISM_ENV] = old_env

    if len(seen) != 4:
        print(f"{FAIL} expected 4 independent buckets, saw {seen!r}")
        return False
    if max_active < 2:
        print(f"{FAIL} expected cross-session parallelism, max_active={max_active}")
        return False
    if elapsed >= 0.45:
        print(f"{FAIL} expected bounded parallel runtime, elapsed={elapsed:.3f}s")
        return False
    print(f"{PASS} independent session buckets integrate in parallel")
    return True


async def _test_same_session_stays_in_one_serial_bucket() -> bool:
    original = run_recovery._integrate_recovered_session_group
    seen: list[list[str]] = []

    async def fake_group(_coordinator, descs, _summary):
        seen.append([str(d["run_id"]) for d in descs])

    run_recovery._integrate_recovered_session_group = fake_group
    try:
        recovered = [
            {"run_id": "older", "app_session_id": "same-session"},
            {"run_id": "newer", "app_session_id": "same-session"},
        ]
        await run_recovery.integrate_recovered_runs(None, recovered)
    finally:
        run_recovery._integrate_recovered_session_group = original

    if seen != [["older", "newer"]]:
        print(f"{FAIL} expected one ordered same-session bucket, saw {seen!r}")
        return False
    print(f"{PASS} same-session recovered runs stay serially bucketed")
    return True


async def _test_double_cancelled_lease_acquire_releases_after_acquisition() -> bool:
    first = run_recovery.RecoveryRootLease("cancel-root").acquire()
    task = asyncio.create_task(run_recovery._acquire_recovery_root_lease("cancel-root"))
    try:
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        first.release()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        first.release()

    next_lease = await asyncio.wait_for(
        run_recovery._acquire_recovery_root_lease("cancel-root"), 2.0,
    )
    await run_recovery._release_recovery_root_lease(next_lease)
    if not task.cancelled():
        print(f"{FAIL} double-cancelled acquisition did not preserve cancellation")
        return False
    print(f"{PASS} double-cancelled acquisition releases after worker acquisition")
    return True


async def _test_same_root_sibling_integrations_serialize() -> bool:
    original_locked = run_recovery._integrate_one_locked
    original_root = run_recovery.session_manager._root_id_for
    active = 0
    max_active = 0

    async def fake_locked(*_args, **_kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.08)
        active -= 1

    run_recovery._integrate_one_locked = fake_locked
    run_recovery.session_manager._root_id_for = lambda _sid: "shared-root"
    try:
        await asyncio.gather(
            run_recovery._integrate_one(None, None, {"app_session_id": "sibling-current"}),
            run_recovery._integrate_one(None, None, {"app_session_id": "sibling-old"}),
        )
    finally:
        run_recovery._integrate_one_locked = original_locked
        run_recovery.session_manager._root_id_for = original_root
    if max_active != 1:
        print(f"{FAIL} same-root sibling integrations overlapped max_active={max_active}")
        return False
    print(f"{PASS} current/old same-root sibling integrations serialize")
    return True


async def _test_double_cancel_joins_mutating_worker_before_lease_release() -> bool:
    original_locked = run_recovery._integrate_one_locked
    original_root = run_recovery.session_manager._root_id_for
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocked_worker():
        entered.set()
        release.wait(2.0)
        finished.set()

    async def fake_locked(*_args, **_kwargs):
        await run_recovery._to_thread_joined(blocked_worker)

    run_recovery._integrate_one_locked = fake_locked
    run_recovery.session_manager._root_id_for = lambda _sid: "worker-root"
    task = asyncio.create_task(
        run_recovery._integrate_one(None, None, {"app_session_id": "replay"})
    )
    competitor = None
    try:
        assert await asyncio.to_thread(entered.wait, 2.0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        competitor = asyncio.create_task(
            run_recovery._acquire_recovery_root_lease("worker-root")
        )
        await asyncio.sleep(0.05)
        assert not competitor.done(), "root lease released before mutating worker joined"
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        lease = await asyncio.wait_for(competitor, 2.0)
        await run_recovery._release_recovery_root_lease(lease)
    finally:
        release.set()
        run_recovery._integrate_one_locked = original_locked
        run_recovery.session_manager._root_id_for = original_root
        if competitor is not None and not competitor.done():
            competitor.cancel()
    if not finished.is_set():
        print(f"{FAIL} cancelled replay/barrier worker outlived root lease")
        return False
    print(f"{PASS} double cancellation joins replay/barrier worker before lease release")
    return True


async def _test_same_root_waiters_cannot_starve_holder_default_executor() -> bool:
    await asyncio.to_thread(lambda: None)
    loop = asyncio.get_running_loop()
    max_workers = loop._default_executor._max_workers
    holder = await run_recovery._acquire_recovery_root_lease("saturation-root")
    completed: list[int] = []

    async def waiter(index: int) -> None:
        lease = await run_recovery._acquire_recovery_root_lease("saturation-root")
        try:
            completed.append(index)
        finally:
            await run_recovery._release_recovery_root_lease(lease)

    waiters = [
        asyncio.create_task(waiter(index))
        for index in range(max_workers + 2)
    ]
    try:
        await asyncio.sleep(0.1)
        holder_work = await asyncio.wait_for(
            run_recovery._to_thread_joined(lambda: "holder-complete"),
            1.0,
        )
        assert holder_work == "holder-complete"
    finally:
        await run_recovery._release_recovery_root_lease(holder)
    await asyncio.wait_for(asyncio.gather(*waiters), 5.0)
    if len(completed) != max_workers + 2:
        print(f"{FAIL} saturation waiters incomplete count={len(completed)}")
        return False
    print(f"{PASS} {max_workers + 2} same-root waiters cannot starve holder executor work")
    return True


async def _test_available_lock_queued_cancel_never_transfers_ownership() -> bool:
    release = threading.Event()
    entered = threading.Event()
    entered_count = 0
    entered_lock = threading.Lock()
    worker_count = run_recovery._RECOVERY_LEASE_EXECUTOR._max_workers

    def blocker() -> None:
        nonlocal entered_count
        with entered_lock:
            entered_count += 1
            if entered_count == worker_count:
                entered.set()
        release.wait(2.0)

    blockers = [
        run_recovery._RECOVERY_LEASE_EXECUTOR.submit(blocker)
        for _ in range(worker_count)
    ]
    task = None
    try:
        assert await asyncio.to_thread(entered.wait, 2.0)
        task = asyncio.create_task(
            run_recovery._acquire_recovery_root_lease("queued-cancel-root")
        )
        await asyncio.sleep(0.05)
        task.cancel()
        task.cancel()
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        for blocker_future in blockers:
            blocker_future.result(timeout=2.0)
    finally:
        release.set()
    probe = run_recovery.RecoveryRootLease("queued-cancel-root").acquire()
    probe.release()
    if task is None or not task.cancelled():
        print(f"{FAIL} queued available-lock cancellation transferred ownership")
        return False
    print(f"{PASS} queued available-lock cancellation leaves no acquired handle")
    return True


def _test_available_lock_queued_shutdown_never_orphans_handle() -> bool:
    backend = str(Path(__file__).resolve().parent.parent)
    code = r'''
import asyncio, threading
import run_recovery as rr

async def main():
    release = threading.Event()
    entered = threading.Event()
    guard = threading.Lock()
    count = 0
    workers = rr._RECOVERY_LEASE_EXECUTOR._max_workers
    def blocker():
        nonlocal count
        with guard:
            count += 1
            if count == workers:
                entered.set()
        release.wait(2.0)
    blockers = [rr._RECOVERY_LEASE_EXECUTOR.submit(blocker) for _ in range(workers)]
    assert await asyncio.to_thread(entered.wait, 2.0)
    acquisition = asyncio.create_task(rr._acquire_recovery_root_lease("queued-shutdown-root"))
    await asyncio.sleep(0.05)
    shutdown = asyncio.create_task(asyncio.to_thread(rr.shutdown_recovery_lease_executor))
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.wait_for(shutdown, 2.0)
    try:
        await acquisition
    except RuntimeError:
        pass
    else:
        raise AssertionError("shutdown transferred queued lease ownership")
    probe = rr.RecoveryRootLease("queued-shutdown-root").acquire()
    probe.release()

asyncio.run(main())
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = backend
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=8.0,
    )
    ok = proc.returncode == 0
    print(f"{PASS if ok else FAIL} queued available-lock shutdown leaves no orphan handle"
          f"{'' if ok else f' stderr={proc.stderr!r}'}")
    return ok


async def _main() -> int:
    ok = True
    ok &= await _test_independent_session_buckets_run_in_parallel()
    ok &= await _test_same_session_stays_in_one_serial_bucket()
    ok &= await _test_double_cancelled_lease_acquire_releases_after_acquisition()
    ok &= await _test_same_root_sibling_integrations_serialize()
    ok &= await _test_double_cancel_joins_mutating_worker_before_lease_release()
    ok &= await _test_same_root_waiters_cannot_starve_holder_default_executor()
    ok &= await _test_available_lock_queued_cancel_never_transfers_ownership()
    ok &= _test_available_lock_queued_shutdown_never_orphans_handle()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
