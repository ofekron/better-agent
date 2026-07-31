"""Regression: worker/fork cleanup is projected from a bus fact.

Run with:
    cd backend && .venv/bin/python scripts/test_worker_fanout_bus.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home
_TMP = _test_home.isolate("bc_worker_fanout_bus_")


async def _run() -> int:
    from event_bus import BusEvent, bus
    from event_bus_subscribers import bind_worker_fanout_cleanup
    from session_manager import manager as session_manager
    from stores import worker_store

    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        print(f"  {'OK' if cond else 'FAIL'}  {label}")
        if not cond:
            failures.append(label)

    broadcasts: list[object] = []
    cancelled: list[str] = []

    async def broadcast_workers_changed(cwd):
        broadcasts.append(cwd)

    async def cancel_session(session_id):
        cancelled.append(session_id)

    bind_worker_fanout_cleanup(broadcast_workers_changed, cancel_session)

    caller = session_manager.create(name="caller", cwd=_TMP, orchestration_mode="native")
    worker = session_manager.create(name="worker", cwd=_TMP, orchestration_mode="native")
    fork = session_manager.create(name="fork", cwd=_TMP, orchestration_mode="native")
    worker_store.set_fork("", caller["id"], worker["id"], fork["id"])

    await bus.publish(BusEvent(
        type="session.worker_fanout_required",
        root_id=worker["id"],
        sid=worker["id"],
        payload={
            "session_id": worker["id"],
            "op_label": "test",
            "caller_scope": False,
            "remove_worker": False,
            "outer_log_msg": "test cleanup failed",
        },
        persist=False,
    ))

    check(
        worker_store.get_fork_record("", caller["id"], worker["id"]) is None,
        "worker fork record cleared from worker_store",
    )
    check(session_manager.get(fork["id"]) is None, "delegate-fork session deleted")
    check(broadcasts == [None], "workers_changed broadcast emitted once")
    check(
        cancelled == [fork["id"]],
        "cancel_session called for the cleared fork before it was deleted "
        "(a live turn on this fork must not be orphaned by cleanup)",
    )

    deleted = session_manager.create(name="deleted", cwd=_TMP, orchestration_mode="native")
    other_worker = session_manager.create(name="other-worker", cwd=_TMP, orchestration_mode="native")
    caller_fork = session_manager.create(name="caller-fork", cwd=_TMP, orchestration_mode="native")
    worker_store.upsert_worker(
        cwd="",
        agent_session_id=deleted["id"],
        orchestration_mode="native",
        agent_sid="agent-deleted",
    )
    worker_store.set_fork("", deleted["id"], other_worker["id"], caller_fork["id"])

    await bus.publish(BusEvent(
        type="session.worker_fanout_required",
        root_id=deleted["id"],
        sid=deleted["id"],
        payload={
            "session_id": deleted["id"],
            "op_label": "session delete",
            "caller_scope": True,
            "remove_worker": True,
            "outer_log_msg": "delete cleanup failed",
        },
        persist=False,
    ))

    check(
        worker_store.get_worker("", deleted["id"]) is None,
        "registered worker record removed on session delete cleanup",
    )
    check(
        worker_store.get_fork_record("", deleted["id"], other_worker["id"]) is None,
        "caller-scope fork record cleared on session delete cleanup",
    )
    check(
        session_manager.get(caller_fork["id"]) is None,
        "caller-scope delegate-fork session deleted on session delete cleanup",
    )
    check(
        cancelled == [fork["id"], caller_fork["id"]],
        "cancel_session called for the caller-scope fork before it was deleted",
    )

    deleted_worker = session_manager.create(
        name="deleted-worker",
        cwd=_TMP,
        orchestration_mode="native",
    )
    worker_side_fork = session_manager.create(
        name="worker-side-fork",
        cwd=_TMP,
        orchestration_mode="native",
    )
    worker_store.upsert_worker(
        cwd="",
        agent_session_id=deleted_worker["id"],
        orchestration_mode="native",
        agent_sid="agent-deleted-worker",
    )
    worker_store.set_fork("", caller["id"], deleted_worker["id"], worker_side_fork["id"])

    await bus.publish(BusEvent(
        type="session.worker_fanout_required",
        root_id=deleted_worker["id"],
        sid=deleted_worker["id"],
        payload={
            "session_id": deleted_worker["id"],
            "op_label": "worker session delete",
            "caller_scope": True,
            "remove_worker": True,
            "outer_log_msg": "worker delete cleanup failed",
        },
        persist=False,
    ))

    check(
        worker_store.get_fork_record("", caller["id"], deleted_worker["id"]) is None,
        "worker-side fork record cleared on registered worker delete",
    )
    check(
        session_manager.get(worker_side_fork["id"]) is None,
        "worker-side delegate-fork session deleted on registered worker delete",
    )
    check(broadcasts == [None, None, None], "workers_changed broadcast emitted for each cleanup")
    check(
        cancelled == [fork["id"], caller_fork["id"], worker_side_fork["id"]],
        "cancel_session called for the worker-side fork before it was deleted",
    )

    responsive = session_manager.create(
        name="responsive-delete",
        cwd=_TMP,
        orchestration_mode="native",
    )
    for index in range(200):
        worker_store.upsert_worker(
            cwd="",
            agent_session_id=f"large-registry-worker-{index}",
            orchestration_mode="native",
            agent_sid=f"large-registry-agent-{index}",
        )
    worker_store.upsert_worker(
        cwd="",
        agent_session_id=responsive["id"],
        orchestration_mode="native",
        agent_sid="responsive-delete-agent",
    )

    entered = threading.Event()
    release = threading.Event()
    loop_progressed = threading.Event()
    observed_progress: list[bool] = []
    real_cleanup = worker_store.cleanup_session_fanout

    def blocking_cleanup(*args, **kwargs):
        result = real_cleanup(*args, **kwargs)
        entered.set()
        release.wait(timeout=1)
        return result

    def release_after_observation() -> None:
        assert entered.wait(timeout=2)
        observed_progress.append(loop_progressed.wait(timeout=0.5))
        release.set()

    worker_store.cleanup_session_fanout = blocking_cleanup
    observer = threading.Thread(target=release_after_observation)
    observer.start()
    try:
        publish_task = asyncio.create_task(bus.publish(BusEvent(
            type="session.worker_fanout_required",
            root_id=responsive["id"],
            sid=responsive["id"],
            payload={
                "session_id": responsive["id"],
                "op_label": "responsive session delete",
                "caller_scope": True,
                "remove_worker": True,
                "outer_log_msg": "responsive delete cleanup failed",
            },
            persist=False,
        )))
        await asyncio.sleep(0)
        loop_progressed.set()
        await publish_task
    finally:
        release.set()
        observer.join(timeout=1)
        worker_store.cleanup_session_fanout = real_cleanup

    check(observed_progress == [True], "large-registry cleanup stays off event loop")
    check(
        worker_store.get_worker("", responsive["id"]) is None,
        "large-registry cleanup remains visible after awaited publish",
    )
    check(broadcasts == [None, None, None, None], "responsive cleanup broadcasts once")

    if failures:
        print(f"\n{len(failures)} FAILURES")
        return 1
    print("\nworker fan-out bus checks OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_run()))
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
