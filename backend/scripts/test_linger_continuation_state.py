"""Regression tests: continuation turns on a lingering runner must be
visible in run/monitoring state.

A lingering babysitter's turn ended long ago (run_state entry removed),
so when background work completes and the CLI re-invokes the model —
a continuation turn — the session used to stay "stopped" while the model
actively streamed. The runner now flips a `continuation_active` run-dir
sentinel on stream transitions, the provider's linger watcher publishes
`run.continuation` FACTS off it, and TurnManager projects those into
is_running / monitoring_state / the background-tick cache.

Locks:
  1. Projection semantics: active fact → is_running True + monitoring
     "active" with NO _run_state entry; inactive fact → stopped.
  2. Cache enumeration: a mid-continuation sid is visible via
     monitoring_state_cached after _refresh_cache (union with
     _linger_continuations — _run_state alone must not gate the cache).
  3. Bus wiring: register_default_subscribers installs the
     run.continuation projection subscriber; a published fact reaches the
     TurnManager projection (recovery's first-poll publish lands only if
     registration precedes recovery — startup does exactly that).
  4. Stuck-projection self-heal: an entry whose runner pid is dead is
     pruned by the background tick (covers a lost inactive fact).
  5. Runner sentinel lifecycle: _LingerStreamState flips the sentinel on
     continuation start / ResultMessage / cap / linger exit.

Run with:
    cd backend && .venv/bin/python scripts/test_linger_continuation_state.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402
_test_home.isolate("bc-test-linger-continuation-")

from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_COORD = None


def _coordinator():
    global _COORD
    if _COORD is None:
        import orchestrator
        _COORD = orchestrator.get_active_coordinator() or orchestrator.Coordinator()
    return _COORD


def _mk_session() -> str:
    sess = session_manager.create(
        name="linger-cont", model="sonnet", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    return sess["id"]


def test_projection_semantics() -> bool:
    tm = _coordinator().turn_manager
    sid = _mk_session()
    run_id = "run-continuation-1"

    if tm.is_running(sid) or tm.monitoring_state(sid) != "stopped":
        print(f"{FAIL} fresh session should be stopped")
        return False

    tm.note_continuation(sid, run_id, True, runner_pid=os.getpid())
    ok = True
    if not tm.is_running(sid):
        print(f"{FAIL} continuation-active session must be running (no _run_state entry)")
        ok = False
    if tm.monitoring_state(sid) != "active":
        print(f"{FAIL} continuation-active monitoring must be 'active', got {tm.monitoring_state(sid)}")
        ok = False
    if tm._run_state.get(sid):
        print(f"{FAIL} projection must not fabricate _run_state entries")
        ok = False

    tm.note_continuation(sid, run_id, False)
    if tm.is_running(sid) or tm.monitoring_state(sid) != "stopped":
        print(f"{FAIL} inactive fact must return the session to stopped")
        ok = False
    if ok:
        print(f"{PASS} projection semantics (active→active/running, inactive→stopped)")
    return ok


def test_cache_sees_continuation() -> bool:
    tm = _coordinator().turn_manager
    sid = _mk_session()
    tm.note_continuation(sid, "run-cache-1", True, runner_pid=os.getpid())
    tm._refresh_cache()
    ok = True
    if not tm.is_running_cached(sid):
        print(f"{FAIL} cache must include continuation-active sid (no _run_state entry)")
        ok = False
    if tm.monitoring_state_cached(sid) != "active":
        print(f"{FAIL} cached monitoring must be 'active', got {tm.monitoring_state_cached(sid)}")
        ok = False
    tm.note_continuation(sid, "run-cache-1", False)
    tm._refresh_cache()
    if tm.is_running_cached(sid):
        print(f"{FAIL} cache must drop the sid after the inactive fact")
        ok = False
    if ok:
        print(f"{PASS} background-tick cache sees mid-continuation sessions")
    return ok


def test_bus_wiring() -> bool:
    from event_bus import BusEvent, bus
    from event_bus_subscribers import register_default_subscribers
    tm = _coordinator().turn_manager
    sid = _mk_session()

    register_default_subscribers()

    async def _drive() -> None:
        await bus.publish(BusEvent(
            type="run.continuation",
            root_id=sid,
            sid=sid,
            payload={
                "app_session_id": sid,
                "run_id": "run-bus-1",
                "active": True,
                "runner_pid": os.getpid(),
            },
            run_id="run-bus-1",
            persist=False,
        ))
        # Bus fan-out schedules subscriber tasks on this loop; yield until
        # the projection lands (bounded).
        for _ in range(100):
            if tm.is_running(sid):
                return
            await asyncio.sleep(0.01)

    asyncio.run(_drive())
    if not tm.is_running(sid) or tm.monitoring_state(sid) != "active":
        print(f"{FAIL} run.continuation fact did not reach the projection")
        return False
    tm.note_continuation(sid, "run-bus-1", False)
    print(f"{PASS} run.continuation bus fact projects into TurnManager")
    return True


def test_dead_pid_prune() -> bool:
    tm = _coordinator().turn_manager
    sid = _mk_session()
    # Spawn-and-reap a real pid so it is guaranteed dead.
    import subprocess
    p = subprocess.Popen(["true"])
    p.wait()
    tm.note_continuation(sid, "run-dead-1", True, runner_pid=p.pid)
    tm.tick_running_state(sid)
    if tm.is_running(sid):
        print(f"{FAIL} dead-pid continuation entry must be pruned by the tick")
        return False
    if tm._linger_continuations.get(sid):
        print(f"{FAIL} stuck projection entry survived the prune")
        return False
    print(f"{PASS} dead-pid continuation entries self-heal via the tick prune")
    return True


def test_runner_sentinel_lifecycle() -> bool:
    from runner import _LingerStreamState
    from claude_agent_sdk import (  # type: ignore
        AssistantMessage, ResultMessage,
    )
    ok = True
    with tempfile.TemporaryDirectory() as td:
        sentinel = Path(td) / "continuation_active"
        st = _LingerStreamState(set(), sentinel_path=sentinel)

        msg = AssistantMessage.__new__(AssistantMessage)
        st.apply(msg)
        if not sentinel.exists():
            print(f"{FAIL} sentinel must exist while a continuation turn is active")
            ok = False

        res = ResultMessage.__new__(ResultMessage)
        st.apply(res)
        if sentinel.exists():
            print(f"{FAIL} sentinel must be removed on ResultMessage")
            ok = False

        st.apply(AssistantMessage.__new__(AssistantMessage))
        st.set_sentinel(False)  # linger-exit finally
        if sentinel.exists():
            print(f"{FAIL} sentinel must be removed on linger exit")
            ok = False
    if ok:
        print(f"{PASS} runner continuation sentinel lifecycle")
    return ok


def main() -> int:
    results = [
        test_projection_semantics(),
        test_cache_sees_continuation(),
        test_bus_wiring(),
        test_dead_pid_prune(),
        test_runner_sentinel_lifecycle(),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
