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

import asyncio
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-running-")

from orchestrator import Coordinator  # noqa: E402
from event_bus import BusEvent, bus  # noqa: E402
import event_bus_subscribers  # noqa: E402
from run_recovery import _normalize_recovered_started_at  # noqa: E402
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


def test_accepted_prompt_fact_fires_active_before_run_registration() -> None:
    async def run() -> None:
        sid = _mk_session()
        fires = _capture()
        coord = _bound_coord()
        await coord.turn_manager.lifecycle.bind()
        event_bus_subscribers.bind_session_lifecycle_projection()
        try:
            await bus.publish(BusEvent(
                type="user_message_requested",
                root_id=sid,
                sid=sid,
                msg_id="accepted-prompt",
                payload={},
                persist=False,
            ))
            assert coord.get_run_state(sid) == []
            active = [
                event for event in fires
                if event.get("kind") == "monitoring_changed"
            ]
            assert active and active[-1]["value"] == "active", active

            await bus.publish(BusEvent(
                type="user_message_failed",
                root_id=sid,
                sid=sid,
                msg_id="accepted-prompt",
                payload={},
                persist=False,
            ))
            assert active[-1]["value"] == "active"
            terminal = [
                event for event in fires
                if event.get("kind") == "monitoring_changed"
            ]
            assert terminal[-1]["value"] == "stopped", terminal
        finally:
            bus.unsubscribe("session_lifecycle_projection")
            await coord.turn_manager.lifecycle.close()

    asyncio.run(run())
    print(f"{PASS} accepted_prompt_fact_fires_active_before_run_registration")


def test_run_start_is_utc_and_recovery_preserves_original_start() -> None:
    sid = _mk_session()
    coord = _bound_coord()
    created = coord.run_state_add(
        sid, run_id="utc-run", kind="native", target_message_id=None,
    )
    created_at = datetime.fromisoformat(created["started_at"])
    assert created_at.tzinfo == timezone.utc, (
        f"new run start must be UTC-aware, got {created['started_at']!r}"
    )
    coord.run_state_remove(sid, "utc-run")

    original_start = "2026-01-02T03:04:05+00:00"
    recovered = coord.run_state_add(
        sid,
        run_id="recovered-run",
        kind="native",
        target_message_id=None,
        started_at=original_start,
    )
    assert recovered["started_at"] == original_start, (
        "recovery must preserve the persisted run start instead of "
        f"restarting elapsed time: {recovered}"
    )
    coord.run_state_remove(sid, "recovered-run")
    print(f"{PASS} run_start_is_utc_and_recovery_preserves_original_start")


def test_legacy_recovery_start_uses_server_local_timezone() -> None:
    if not hasattr(time, "tzset"):
        print(f"{PASS} legacy_recovery_start_uses_server_local_timezone (unsupported)")
        return
    original_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Asia/Jerusalem"
        time.tzset()
        normalized = _normalize_recovered_started_at(
            "2026-01-02T03:04:05",
        )
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()
    assert normalized == "2026-01-02T01:04:05+00:00", (
        "legacy naive timestamps must be interpreted in the server-local "
        f"timezone before conversion to UTC, got {normalized!r}"
    )
    print(f"{PASS} legacy_recovery_start_uses_server_local_timezone")


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


def main() -> int:
    try:
        test_run_start_fires_running_true()
        test_accepted_prompt_fact_fires_active_before_run_registration()
        test_run_start_is_utc_and_recovery_preserves_original_start()
        test_legacy_recovery_start_uses_server_local_timezone()
        test_multiple_runs_single_fire()
        test_worker_fork_does_not_set_running()
        test_active_pidless_turn_survives_prune()
        test_new_pidless_worker_survives_before_pid_attach()
        test_duplicate_worker_run_id_updates_existing_entry()
        test_audit_running_discrepancy_records_state_layers()
        print("ALL PASSED")
        return 0
    except AssertionError as e:
        print(f"{FAIL}: {e}")
        return 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
