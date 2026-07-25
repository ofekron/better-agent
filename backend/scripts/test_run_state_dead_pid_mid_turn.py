"""Regression tests for the mid-turn run_state drop that blanked the
"Running…" badge.

Bug: a provider subprocess exits between attempts (context continuation
after PROVIDER_CAPABILITIES_CHANGED_ERROR, context overflow, rate limit,
transient retry). The `_run_state` entry keeps the now-dead pid until the
retry handler resumes — observed 5.7 s later in production. The 2 s
background tick's `_prune_dead_entries` dropped the entry unconditionally
on a dead pid, and nothing ever restored it: the next attempt's
`run_state_set_pid` finds no matching run_id and no-ops. `is_running` went
False for the rest of the turn, so the frontend showed no run badge while
the provider kept streaming agent output.

Locks the invariant: while a turn is in flight, its coroutine owns the
run entry. The pruner is orphan GC for crashed/detached runs only.

Run with:
    cd backend && .venv/bin/python scripts/test_run_state_dead_pid_mid_turn.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402
_TMP_HOME = _test_home.isolate("bc-test-run-state-dead-pid-")  # noqa: E402

from turn_manager import TurnManager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_results: list[tuple[str, bool, str]] = []

SID = "sess-under-test"
TURN_RUN_ID = "turn-run-1"


def _check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))
    print(f"  {PASS if cond else FAIL} {name}" + (f" — {detail}" if detail and not cond else ""))


def _dead_pid() -> int:
    """A pid that is guaranteed reaped. `subprocess` rather than `os.fork`
    so the test runs on Windows too."""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


def _manager() -> TurnManager:
    # `_prune_dead_entries` only reaches Coordinator for entries it drops
    # (via `_maybe_flip_streaming`, which returns early on a null target),
    # so a bare stub back-reference is enough for these unit checks.
    return TurnManager(object())


def _seed(
    tm: TurnManager,
    *,
    pid: int | None,
    in_flight: bool,
    started_at: datetime,
    retrying: bool = False,
) -> None:
    entry: dict = {
        "run_id": TURN_RUN_ID,
        "kind": "native",
        "target_message_id": None,
        "delegation_id": None,
        "started_at": started_at.isoformat(),
        "last_event_at": started_at.isoformat(),
    }
    if pid is not None:
        entry["pid"] = pid
    if retrying:
        entry["retrying"] = True
    tm._run_state[SID] = [entry]
    tm.active_run_ids[SID] = [TURN_RUN_ID]
    if in_flight:
        tm.cancel_events[SID] = asyncio.Event()


def test_dead_pid_survives_while_turn_in_flight() -> None:
    """A — the repro. Subprocess exited, no retry handler has run yet."""
    tm = _manager()
    _seed(
        tm,
        pid=_dead_pid(),
        in_flight=True,
        # Older than the pidless staleness window, so the entry's survival
        # cannot be attributed to the age grace period.
        started_at=datetime.now(timezone.utc) - timedelta(seconds=120),
    )

    tm._prune_dead_entries(SID)

    _check(
        "entry survives a dead pid while the turn is in flight",
        len(tm._run_state.get(SID, [])) == 1,
        f"run_state={tm._run_state.get(SID)}",
    )
    _check(
        "is_running stays True across the between-attempts gap",
        tm.is_running(SID) is True,
    )
    _check(
        "run_id stays in active_run_ids",
        TURN_RUN_ID in (tm.active_run_ids.get(SID) or []),
    )


def test_dead_pid_orphan_is_still_collected() -> None:
    """B — the gate must not become a leak for crashed/detached runs."""
    tm = _manager()
    _seed(
        tm,
        pid=_dead_pid(),
        in_flight=False,
        started_at=datetime.now(timezone.utc) - timedelta(seconds=120),
    )

    tm._prune_dead_entries(SID)

    _check(
        "orphan entry with a dead pid is dropped",
        tm._run_state.get(SID) in (None, []),
        f"run_state={tm._run_state.get(SID)}",
    )
    _check("is_running False for the orphan", tm.is_running(SID) is False)


def test_worker_entry_is_not_shielded_by_the_parent_turn() -> None:
    """B2 — the ownership gate must not extend to worker runs.

    Worker entries are keyed `worker-<delegation_id>` while `active_run_ids`
    carries the provider run id, so they never satisfy the ownership
    predicate. That mismatch is what keeps worker semantics unchanged; lock
    it, because unifying the two id namespaces would otherwise leave a
    crashed worker subprocess "running" for the rest of the parent turn.
    """
    tm = _manager()
    started = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    tm._run_state[SID] = [{
        "run_id": "worker-deleg-1",
        "kind": "worker",
        "target_message_id": None,
        "delegation_id": "deleg-1",
        "pid": _dead_pid(),
        "started_at": started,
        "last_event_at": started,
    }]
    # Parent turn genuinely in flight, but it owns a DIFFERENT run id.
    tm.active_run_ids[SID] = [TURN_RUN_ID]
    tm.cancel_events[SID] = asyncio.Event()

    tm._prune_dead_entries(SID)

    _check(
        "dead worker run is dropped even while the parent turn is in flight",
        tm._run_state.get(SID) in (None, []),
        f"run_state={tm._run_state.get(SID)}",
    )


def test_begin_retry_keeps_the_run_visible() -> None:
    """C — the between-attempts helper's own contract."""
    tm = _manager()
    emitted: list[str] = []

    async def _fake_emit(sid: str) -> None:
        emitted.append(sid)

    tm.emit_run_state = _fake_emit  # type: ignore[method-assign]
    _seed(
        tm,
        pid=_dead_pid(),
        in_flight=True,
        started_at=datetime.now(timezone.utc) - timedelta(seconds=120),
    )

    asyncio.run(tm.run_state_begin_retry(SID, TURN_RUN_ID))

    entry = (tm._run_state.get(SID) or [{}])[0]
    _check("begin_retry drops the stale pid", "pid" not in entry, str(entry))
    _check("begin_retry marks the entry retrying", entry.get("retrying") is True)
    _check("begin_retry broadcasts the new run state", emitted == [SID])

    tm._prune_dead_entries(SID)
    _check(
        "a retrying pidless entry survives pruning",
        len(tm._run_state.get(SID, [])) == 1,
        f"run_state={tm._run_state.get(SID)}",
    )
    _check("is_running stays True while retrying", tm.is_running(SID) is True)


def _main() -> int:
    print("Test A — dead pid mid-turn (the reported bug)")
    test_dead_pid_survives_while_turn_in_flight()
    print("Test B — dead pid with no turn in flight is still collected")
    test_dead_pid_orphan_is_still_collected()
    print("Test B2 — worker run is not shielded by the parent turn")
    test_worker_entry_is_not_shielded_by_the_parent_turn()
    print("Test C — run_state_begin_retry contract")
    test_begin_retry_keeps_the_run_visible()

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
