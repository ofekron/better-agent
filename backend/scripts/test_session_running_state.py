"""Locks the per-session `is_running` flag:

1. `coordinator.run_state_add` calls `session_manager.recompute_running`,
   which computes `coordinator.is_running(sid)` live and broadcasts
   `running_changed{value:True}` on the first add (False→True diff).
2. Multiple concurrent runs on the same sid fire `running_changed`
   ONLY ONCE (subsequent recomputes see True→True and dedup).
3. `run_state_remove` only flips to False when the LAST run leaves
   (live recompute returns False only when `_run_state[sid]` is empty
   OR all surviving entries have dead pids / no owning task).
4. Worker forks (`kind != "user"`) do NOT flip the running flag —
   the user-facing sidebar/home badge stays clean.

The canonical "running" signal is `run_state_add`/`run_state_remove`
+ the periodic `tick_running_state` (silent pid-death detection),
which also drives the live + recovery paths in run_recovery — meaning
crash recovery's call to `run_state_add` for a `live_no_rehook` run
will fire `running_changed:true` for free. This is asserted by
calling `run_state_add` directly with the same shape recovery uses.

Run with:
    cd backend && .venv/bin/python scripts/test_session_running_state.py
"""

from __future__ import annotations

import os
import asyncio
import shutil
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-running-")

from orchestrator import Coordinator  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _mk_session() -> str:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp/test-running",
        orchestration_mode="native", source="cli",
    )
    return sess["id"]


def _capture() -> list[dict]:
    events: list[dict] = []

    def listener(sid: str, change: dict) -> None:
        events.append({"sid": sid, **change})

    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        session_manager.add_listener(listener)
    return events


def _bound_coord() -> "Coordinator":
    """Construct a coord and bind its `is_running` into session_manager
    so the delegated `session_manager.is_running` / `recompute_running`
    path resolves against THIS coord's `_run_state`. Mirrors what
    `main.py` does at module load (`bind_running_check`)."""
    coord = Coordinator()
    session_manager.bind_running_check(coord.is_running)
    session_manager.bind_monitoring_check(coord.turn_manager.monitoring_state)
    return coord


def test_run_start_fires_running_true() -> None:
    sid = _mk_session()
    fires = _capture()
    coord = _bound_coord()
    coord.run_state_add(
        sid, run_id="r1", kind="native",
        target_message_id=None,
    )
    rc = [f for f in fires if f.get("kind") == "running_changed"]
    assert len(rc) == 1 and rc[0]["value"] is True, (
        f"expected one running_changed:True, got {rc}"
    )
    assert session_manager.is_running(sid) is True
    coord.run_state_remove(sid, "r1")
    print(f"{PASS} run_start_fires_running_true")


def test_multiple_runs_single_fire() -> None:
    sid = _mk_session()
    fires = _capture()
    coord = _bound_coord()
    coord.run_state_add(sid, run_id="r1", kind="native", target_message_id=None)
    coord.run_state_add(sid, run_id="r2", kind="worker", target_message_id=None)
    coord.run_state_add(sid, run_id="r3", kind="worker", target_message_id=None)
    rc_true = [f for f in fires if f.get("kind") == "running_changed" and f.get("value")]
    assert len(rc_true) == 1, (
        f"multiple run_state_add must fire running_changed:True only "
        f"on the first add; got {rc_true}"
    )
    # Remove all — only the LAST remove should flip to False.
    rc_pre = len([f for f in fires if f.get("kind") == "running_changed" and not f.get("value")])
    coord.run_state_remove(sid, "r1")
    coord.run_state_remove(sid, "r2")
    rc_mid = len([f for f in fires if f.get("kind") == "running_changed" and not f.get("value")])
    assert rc_mid == rc_pre, (
        "intermediate run_state_remove must NOT flip running:False while "
        "another run is still alive"
    )
    coord.run_state_remove(sid, "r3")
    rc_post = [f for f in fires if f.get("kind") == "running_changed" and not f.get("value")]
    assert len(rc_post) == 1, (
        f"final run_state_remove must fire running_changed:False once, "
        f"got {rc_post}"
    )
    assert session_manager.is_running(sid) is False
    print(f"{PASS} multiple_runs_single_fire")


def test_worker_fork_does_not_set_running() -> None:
    sid = _mk_session()
    fork = session_manager.create_delegate_fork(
        parent_agent_session_id=sid,
        caller_agent_session_id=sid,
        parent_agent_sid_at_fork="fake-sid",
        parent_line_count_at_fork=0,
        orchestration_mode="native",
    )
    fork_id = fork["id"]
    session_manager._roots.pop(sid, None)
    fires = _capture()
    coord = _bound_coord()
    coord.run_state_add(fork_id, run_id="r-fork", kind="worker", target_message_id=None)
    rc = [f for f in fires if f.get("kind") == "running_changed"]
    assert len(rc) == 0, (
        f"worker-fork run must not surface running_changed; got {rc}"
    )
    assert session_manager.is_running(fork_id) is False
    coord.run_state_remove(fork_id, "r-fork")
    print(f"{PASS} worker_fork_does_not_set_running")


def test_active_pidless_turn_survives_prune() -> None:
    sid = _mk_session()
    coord = _bound_coord()
    coord.active_run_ids[sid] = ["r-slow"]
    coord.cancel_events[sid] = threading.Event()
    coord.run_state_add(sid, run_id="r-slow", kind="native", target_message_id=None)
    coord._run_state[sid][0]["started_at"] = "2000-01-01T00:00:00"

    pruned = coord._prune_dead_entries(sid)
    assert pruned is False, "active pidless turn must not be pruned"
    assert coord.get_run_state(sid), "run_state disappeared before pid arrived"

    coord.run_state_set_pid(sid, "r-slow", os.getpid())
    runs = coord.get_run_state(sid)
    assert runs and runs[0].get("pid") == os.getpid(), (
        f"pid update did not attach to surviving run_state: {runs}"
    )
    coord.cancel_events.pop(sid, None)
    coord.run_state_remove(sid, "r-slow")
    print(f"{PASS} active_pidless_turn_survives_prune")


def test_new_pidless_worker_survives_before_pid_attach() -> None:
    sid = _mk_session()
    coord = _bound_coord()
    coord.run_state_add(sid, run_id="worker-race", kind="worker", target_message_id=None)

    pruned = coord._prune_dead_entries(sid)
    assert pruned is False, "new pidless worker must survive initial prune window"

    coord.run_state_set_pid(sid, "worker-race", os.getpid())
    runs = coord.get_run_state(sid)
    assert runs and runs[0].get("pid") == os.getpid(), (
        f"pid update did not attach to worker run_state: {runs}"
    )
    coord.run_state_remove(sid, "worker-race")
    print(f"{PASS} new_pidless_worker_survives_before_pid_attach")


def test_duplicate_worker_run_id_updates_existing_entry() -> None:
    sid = _mk_session()
    coord = _bound_coord()
    coord.run_state_add(
        sid,
        run_id="worker-same",
        kind="worker",
        target_message_id="msg-1",
        delegation_id="del-1",
    )
    coord.run_state_add(
        sid,
        run_id="worker-same",
        kind="worker",
        target_message_id="msg-1",
        delegation_id="del-1",
        pid=os.getpid(),
    )
    runs = coord.get_run_state(sid)
    assert len(runs) == 1, f"duplicate worker run_id must not append: {runs}"
    assert runs[0].get("pid") == os.getpid(), f"pid not updated: {runs}"
    coord.run_state_remove(sid, "worker-same")
    print(f"{PASS} duplicate_worker_run_id_updates_existing_entry")


def test_audit_running_discrepancy_records_state_layers() -> None:
    sid = _mk_session()
    coord = _bound_coord()
    coord.run_state_add(
        sid,
        run_id="audit-run",
        kind="native",
        target_message_id=None,
        pid=os.getpid(),
    )

    records = coord.turn_manager.audit_running_discrepancies()
    matching = [r for r in records if r.get("sid") == sid[:8]]
    assert len(matching) == 1, f"expected one audit record for {sid}: {records}"

    record = matching[0]
    assert record["live_is_running"] is True, record
    assert record["cached_is_running"] is False, record
    assert record["live_monitoring"] == "idle", record
    assert record["cached_monitoring"] == "stopped", record
    assert "cached!=live" in record["reasons"], record
    assert "cached_monitoring!=live" in record["reasons"], record
    assert record["runs"][0]["pid_alive"] is True, record

    coord.run_state_remove(sid, "audit-run")
    print(f"{PASS} audit_running_discrepancy_records_state_layers")


def test_active_precedence_masks_background_signal() -> None:
    sid = _mk_session()
    coord = _bound_coord()
    tm = coord.turn_manager
    tm.run_state_add(
        sid,
        run_id="background-join",
        kind="native",
        target_message_id=None,
    )
    tm.active_run_ids[sid] = ["background-join"]
    tm.cancel_events[sid] = threading.Event()
    tm._run_state[sid][0]["foreground_status"] = "completed"
    original = tm._has_background_work
    tm._has_background_work = lambda candidate_sid: candidate_sid == sid
    try:
        assert tm.monitoring_state(sid) == "waiting_on_background", (
            "a known background-only join must not be reported as active"
        )
    finally:
        tm._has_background_work = original
        tm.cancel_events.pop(sid, None)
        tm.active_run_ids.pop(sid, None)
        tm.run_state_remove(sid, "background-join")
    print(f"{PASS} active_precedence_masks_background_signal")


def test_activity_projection_is_monotonic() -> None:
    sid = _mk_session()
    coord = _bound_coord()
    tm = coord.turn_manager
    tm.run_state_add(
        sid,
        run_id="activity-run",
        kind="native",
        activity_revision=1,
        turn_id="turn-1",
    )
    assert tm.run_state_apply_activity(
        sid,
        "activity-run",
        foreground_status="completed",
        background_work_ids=["task-1"],
        activity_revision=2,
        turn_id="turn-1",
    ) is True
    assert tm.monitoring_state(sid) == "waiting_on_background"
    assert tm.has_active_runs(sid) is False
    assert tm.run_state_apply_activity(
        sid,
        "activity-run",
        foreground_status="running",
        background_work_ids=[],
        activity_revision=1,
        turn_id="turn-1",
    ) is False
    run = tm.get_run_state(sid)[0]
    assert run["foreground_status"] == "completed"
    assert run["background_work_ids"] == ["task-1"]
    tm.run_state_remove(sid, "activity-run")
    print(f"{PASS} activity_projection_is_monotonic")


def test_foreground_activity_outranks_older_background_work() -> None:
    sid = _mk_session()
    coord = _bound_coord()
    tm = coord.turn_manager
    tm.run_state_add(
        sid,
        run_id="old-background",
        kind="native",
        foreground_status="completed",
        background_work_ids=["task-1"],
        activity_revision=2,
    )
    tm.run_state_add(sid, run_id="new-foreground", kind="native")
    tm.active_run_ids[sid] = ["old-background", "new-foreground"]
    assert tm.monitoring_state(sid) == "active"
    tm.active_run_ids.pop(sid, None)
    tm.run_state_remove(sid, "old-background")
    tm.run_state_remove(sid, "new-foreground")
    print(f"{PASS} foreground_activity_outranks_older_background_work")


def test_releasing_foreground_retains_background_until_runner_exits() -> None:
    sid = _mk_session()
    coord = _bound_coord()
    tm = coord.turn_manager
    tm.run_state_add(
        sid,
        run_id="retained-background",
        kind="native",
        pid=os.getpid(),
        foreground_status="completed",
        background_work_ids=["child:1"],
        activity_revision=2,
    )
    tm.active_run_ids[sid] = ["retained-background"]
    tm.run_state_release_foreground(sid, "retained-background")
    assert tm.get_run_state(sid), "background run disappeared at foreground completion"
    assert tm.has_active_runs(sid) is False
    assert tm.monitoring_state(sid) == "waiting_on_background"
    tm.active_run_ids.pop(sid, None)
    tm.run_state_remove(sid, "retained-background")
    print(f"{PASS} releasing_foreground_retains_background_until_runner_exits")


def test_detached_background_links_are_lifecycle_scoped() -> None:
    sid = _mk_session()
    coord = _bound_coord()
    tm = coord.turn_manager
    target = _mk_session()
    tm.register_detached_background(
        parent_session_id=sid,
        target_session_id=target,
        lifecycle_msg_id="lifecycle-1",
    )
    tm.register_detached_background(
        parent_session_id=sid,
        target_session_id=target,
        lifecycle_msg_id="lifecycle-2",
    )
    assert tm.monitoring_state(sid) == "waiting_on_background"
    assert len(tm.get_run_state(sid)) == 2

    tm.run_state_add(sid, run_id="new-foreground", kind="native")
    tm.active_run_ids[sid] = ["new-foreground"]
    assert tm.monitoring_state(sid) == "active"
    tm.active_run_ids.pop(sid, None)
    tm.run_state_remove(sid, "new-foreground")
    assert tm.monitoring_state(sid) == "waiting_on_background"

    tm.clear_detached_background(
        lifecycle_msg_id="lifecycle-1",
        target_session_id=target,
    )
    assert tm.monitoring_state(sid) == "waiting_on_background"
    assert len(tm.get_run_state(sid)) == 1
    tm.clear_detached_background(
        lifecycle_msg_id="lifecycle-2",
        target_session_id=target,
    )
    assert tm.monitoring_state(sid) == "stopped"
    tm.register_detached_background(
        parent_session_id=sid,
        target_session_id=target,
        lifecycle_msg_id="lifecycle-delete",
    )
    tm.drop_detached_background_for_sessions({target})
    assert tm.monitoring_state(sid) == "stopped"
    print(f"{PASS} detached_background_links_are_lifecycle_scoped")


def test_cancel_turn_with_detached_cancels_only_linked_lifecycle() -> None:
    parent = _mk_session()
    target = _mk_session()
    coord = _bound_coord()
    tm = coord.turn_manager
    linked = {
        "id": "linked-queued-id",
        "lifecycle_msg_id": "linked-lifecycle",
        "source": "delegate_task",
    }
    unrelated = {
        "id": "unrelated-queued-id",
        "lifecycle_msg_id": "unrelated-lifecycle",
    }
    session_manager.add_queued_prompt(target, linked)
    session_manager.add_queued_prompt(target, unrelated)
    queue = asyncio.Queue()
    queue.put_nowait({"_queued_id": linked["id"], **linked})
    queue.put_nowait({"_queued_id": unrelated["id"], **unrelated})
    coord._prompt_queues[target] = queue
    coord._queued_ids[target] = [linked["id"], unrelated["id"]]
    tm.register_detached_background(
        parent_session_id=parent,
        target_session_id=target,
        lifecycle_msg_id=linked["lifecycle_msg_id"],
    )

    assert asyncio.run(tm.cancel_turn_with_detached(parent)) is True
    queued = (session_manager.get(target) or {}).get("queued_prompts") or []
    assert [item["id"] for item in queued] == [unrelated["id"]]
    assert tm.monitoring_state(parent) == "stopped"
    print(f"{PASS} cancel_turn_with_detached_cancels_only_linked_lifecycle")


def test_run_state_snapshot_and_publish_are_serialized() -> None:
    sid = _mk_session()
    coord = _bound_coord()
    tm = coord.turn_manager
    published: list[list[str]] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def broadcast(_sid, _type, data, **_kwargs):
        if not published:
            first_started.set()
            await release_first.wait()
        published.append([run["run_id"] for run in data["runs"]])

    coord.broadcast_session = broadcast

    async def scenario() -> None:
        tm.run_state_add(sid, run_id="old", kind="native")
        first_emit = asyncio.create_task(tm.emit_run_state(sid))
        await first_started.wait()
        tm.run_state_remove(sid, "old")
        tm.run_state_add(sid, run_id="new", kind="native")
        second_emit = asyncio.create_task(tm.emit_run_state(sid))
        await asyncio.sleep(0)
        release_first.set()
        await asyncio.gather(first_emit, second_emit)

    asyncio.run(scenario())
    assert published == [["old"], ["new"]], published
    tm.run_state_remove(sid, "new")
    print(f"{PASS} run_state_snapshot_and_publish_are_serialized")


def main() -> int:
    try:
        test_run_start_fires_running_true()
        test_multiple_runs_single_fire()
        test_worker_fork_does_not_set_running()
        test_active_pidless_turn_survives_prune()
        test_new_pidless_worker_survives_before_pid_attach()
        test_duplicate_worker_run_id_updates_existing_entry()
        test_audit_running_discrepancy_records_state_layers()
        test_active_precedence_masks_background_signal()
        test_activity_projection_is_monotonic()
        test_foreground_activity_outranks_older_background_work()
        test_releasing_foreground_retains_background_until_runner_exits()
        test_detached_background_links_are_lifecycle_scoped()
        test_cancel_turn_with_detached_cancels_only_linked_lifecycle()
        test_run_state_snapshot_and_publish_are_serialized()
        print("ALL PASSED")
        return 0
    except AssertionError as e:
        print(f"{FAIL}: {e}")
        return 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
