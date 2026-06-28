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
import shutil
import sys
import tempfile
import threading

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-running-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

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


def main() -> int:
    try:
        test_run_start_fires_running_true()
        test_multiple_runs_single_fire()
        test_worker_fork_does_not_set_running()
        test_active_pidless_turn_survives_prune()
        test_duplicate_worker_run_id_updates_existing_entry()
        print("ALL PASSED")
        return 0
    except AssertionError as e:
        print(f"{FAIL}: {e}")
        return 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
