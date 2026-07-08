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


class _FakePopen:
    """poll()-shaped stand-in for the runner process."""

    def __init__(self) -> None:
        self.pid = os.getpid()
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class _BusCapture:
    """Collect run.lingering / run.continuation facts off the bus."""

    def __init__(self, name: str) -> None:
        from event_bus import bus
        self.name = name
        self.events: list[tuple[str, dict]] = []

        async def _cap(ev) -> None:
            self.events.append((ev.type, dict(ev.payload)))

        bus.subscribe("run.lingering", _cap, name=name)
        bus.subscribe("run.continuation", _cap, name=name)

    def close(self) -> None:
        from event_bus import bus
        bus.unsubscribe(self.name)

    def has(self, etype: str, **payload_match) -> bool:
        return any(
            t == etype and all(p.get(k) == v for k, v in payload_match.items())
            for t, p in self.events
        )


async def _wait_until(pred, timeout: float = 5.0) -> bool:
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


def _mk_provider():
    from provider_claude import ClaudeProvider
    return ClaudeProvider({"id": "test-linger-watch"})


def test_watcher_sentinel_seam() -> bool:
    """_watch_linger_exit end-to-end: sentinel flips on disk drive the
    run.lingering / run.continuation publishes, and process exit runs the
    closing epilogue (inactive publishes + deregistration + released)."""
    from provider_claude import RunState

    provider = _mk_provider()
    sid = _mk_session()
    cap = _BusCapture("test-watch-seam")
    ok = True
    try:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            popen = _FakePopen()
            rs = RunState(
                run_id="run-watch-1", run_dir=run_dir, popen=popen,
                mode="native", app_session_id=sid, queue=asyncio.Queue(),
            )

            async def _drive() -> bool:
                inner_ok = True
                provider._runs[rs.run_id] = rs
                task = asyncio.get_event_loop().create_task(
                    provider._watch_linger_exit(rs),
                )
                (run_dir / "lingering").touch()
                if not await _wait_until(
                    lambda: cap.has("run.lingering", lingering=True),
                ):
                    print(f"{FAIL} lingering sentinel did not publish run.lingering")
                    inner_ok = False
                (run_dir / "continuation_active").touch()
                if not await _wait_until(
                    lambda: cap.has(
                        "run.continuation", active=True, runner_pid=popen.pid,
                    ),
                ):
                    print(f"{FAIL} continuation sentinel did not publish active fact")
                    inner_ok = False
                (run_dir / "continuation_active").unlink()
                if not await _wait_until(
                    lambda: cap.has("run.continuation", active=False),
                ):
                    print(f"{FAIL} sentinel removal did not publish inactive fact")
                    inner_ok = False
                popen.returncode = 0
                await asyncio.wait_for(task, timeout=10.0)
                if not cap.has("run.lingering", lingering=False):
                    print(f"{FAIL} exit epilogue did not publish lingering=False")
                    inner_ok = False
                if rs.run_id in provider._runs:
                    print(f"{FAIL} run stayed registered after watcher exit")
                    inner_ok = False
                if not rs.released.is_set():
                    print(f"{FAIL} released gate not set after watcher exit")
                    inner_ok = False
                return inner_ok

            ok = asyncio.run(_drive())
    finally:
        cap.close()
    if ok:
        print(f"{PASS} watcher sentinel seam (flips → facts, exit → epilogue)")
    return ok


def test_watcher_recovery_stub() -> bool:
    """Recovery re-attaches lingering runs as attribute stubs without a
    `continuation_active` field; a sentinel already on disk at watch start
    (backend restarted mid-continuation) must publish on the FIRST poll."""
    from types import SimpleNamespace

    provider = _mk_provider()
    sid = _mk_session()
    cap = _BusCapture("test-watch-stub")
    ok = True
    try:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            popen = _FakePopen()
            (run_dir / "continuation_active").touch()
            rs = SimpleNamespace(
                run_id="run-watch-stub-1", run_dir=run_dir, popen=popen,
                app_session_id=sid, lingering=False, tailer=None,
                tailer_task=None, jsonl_path=None,
                released=asyncio.Event(),
            )

            async def _drive() -> bool:
                inner_ok = True
                provider._runs[rs.run_id] = rs
                task = asyncio.get_event_loop().create_task(
                    provider._watch_linger_exit(rs),
                )
                if not await _wait_until(
                    lambda: cap.has("run.continuation", active=True),
                ):
                    print(f"{FAIL} pre-existing sentinel did not publish on first poll")
                    inner_ok = False
                popen.returncode = 0
                await asyncio.wait_for(task, timeout=10.0)
                if not cap.has("run.continuation", active=False):
                    print(f"{FAIL} stub epilogue did not publish inactive fact")
                    inner_ok = False
                if rs.run_id in provider._runs or not rs.released.is_set():
                    print(f"{FAIL} stub run not deregistered/released on exit")
                    inner_ok = False
                return inner_ok

            ok = asyncio.run(_drive())
    finally:
        cap.close()
    if ok:
        print(f"{PASS} recovery-stub watcher (first-poll publish, getattr path)")
    return ok


def test_watcher_cancel_still_cleans() -> bool:
    """Regression: task cancellation delivered while the closing publish
    is in flight must not skip `_cleanup_run` — a skipped cleanup leaves
    `released` unset and wedges start_run's linger-serialization gate."""
    from event_bus import bus
    from provider_claude import RunState

    provider = _mk_provider()
    sid = _mk_session()
    ok = True
    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        popen = _FakePopen()
        rs = RunState(
            run_id="run-watch-cancel-1", run_dir=run_dir, popen=popen,
            mode="native", app_session_id=sid, queue=asyncio.Queue(),
        )

        async def _drive() -> bool:
            inner_ok = True
            provider._runs[rs.run_id] = rs
            task = asyncio.get_event_loop().create_task(
                provider._watch_linger_exit(rs),
            )
            (run_dir / "continuation_active").touch()
            if not await _wait_until(lambda: rs.continuation_active):
                print(f"{FAIL} watcher never mirrored the continuation sentinel")
                return False

            # Wedge the bus so the finally's inactive publish blocks,
            # then land a second cancel inside it.
            orig_publish = bus.publish
            hung = asyncio.Event()
            calls = {"n": 0}

            async def _hanging_publish(event, **kw) -> None:
                calls["n"] += 1
                await hung.wait()

            bus.publish = _hanging_publish  # type: ignore[method-assign]
            try:
                task.cancel()
                if not await _wait_until(lambda: calls["n"] >= 1):
                    print(f"{FAIL} finally never reached the closing publish")
                    inner_ok = False
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=10.0)
                except asyncio.CancelledError:
                    pass
            finally:
                bus.publish = orig_publish  # type: ignore[method-assign]
                hung.set()

            if rs.run_id in provider._runs:
                print(f"{FAIL} cancellation skipped _cleanup_run (run still registered)")
                inner_ok = False
            if not rs.released.is_set():
                print(f"{FAIL} cancellation left the released gate unset")
                inner_ok = False
            return inner_ok

        ok = asyncio.run(_drive())
    if ok:
        print(f"{PASS} cancelled watcher still deregisters + releases the run")
    return ok


class _FlakyClient:
    """receive_messages(): first call raises mid-stream (transient error),
    second call yields one continuation message then blocks like a live
    idle stream."""

    def __init__(self) -> None:
        self.calls = 0

    def receive_messages(self):
        self.calls += 1
        if self.calls == 1:
            return self._gen_error()
        return self._gen_live()

    async def _gen_error(self):
        raise RuntimeError("transient stream hiccup")
        yield  # pragma: no cover — makes this an async generator

    async def _gen_live(self):
        from claude_agent_sdk import AssistantMessage  # type: ignore
        yield AssistantMessage.__new__(AssistantMessage)
        await asyncio.Event().wait()


class _BrokenClient:
    """receive_messages() raises on every call — a truly broken stream."""

    def receive_messages(self):
        return self._gen()

    async def _gen(self):
        raise RuntimeError("stream permanently broken")
        yield  # pragma: no cover


def test_drain_survives_transient_error() -> bool:
    """Regression: a transient stream error must NOT end the drain — the
    linger gates subagent/continuation busyness on drain-aliveness, so a
    dead drain reaps claude + MCP while background work is mid-flight."""
    import logging
    from runner import _drain_background_tasks, _LingerStreamState

    ok = True
    with tempfile.TemporaryDirectory() as td:
        sentinel = Path(td) / "continuation_active"
        st = _LingerStreamState(set(), sentinel_path=sentinel)
        client = _FlakyClient()

        async def _drive() -> bool:
            task = asyncio.get_event_loop().create_task(
                _drain_background_tasks(client, st, logging.getLogger("test")),
            )
            # Restart backoff is 0.5s; the message on the restarted stream
            # flips the continuation sentinel — the observable proof the
            # drain survived the error and kept consuming.
            ok_inner = await _wait_until(lambda: sentinel.exists(), timeout=5.0)
            if not ok_inner:
                print(f"{FAIL} drain died on a transient stream error")
            if task.done():
                print(f"{FAIL} drain task ended while the stream is still live")
                ok_inner = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return ok_inner

        ok = asyncio.run(_drive())
    if ok:
        print(f"{PASS} drain restarts across a transient stream error")
    return ok


def test_drain_gives_up_after_sustained_failure() -> bool:
    """A permanently broken stream must end the drain (bounded restarts) —
    otherwise a wedged client pins the linger forever."""
    import logging
    from runner import _drain_background_tasks, _LingerStreamState

    async def _drive() -> bool:
        st = _LingerStreamState(set())
        task = asyncio.get_event_loop().create_task(
            _drain_background_tasks(
                _BrokenClient(), st, logging.getLogger("test"),
            ),
        )
        # 5 failures × 0.5s backoff — bounded well under this timeout.
        done = await _wait_until(lambda: task.done(), timeout=10.0)
        if not done:
            print(f"{FAIL} drain never gave up on a permanently broken stream")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return False
        return True

    ok = asyncio.run(_drive())
    if ok:
        print(f"{PASS} drain gives up after sustained stream failure")
    return ok


def main() -> int:
    results = [
        test_projection_semantics(),
        test_cache_sees_continuation(),
        test_bus_wiring(),
        test_dead_pid_prune(),
        test_runner_sentinel_lifecycle(),
        test_watcher_sentinel_seam(),
        test_watcher_recovery_stub(),
        test_watcher_cancel_still_cleans(),
        test_drain_survives_transient_error(),
        test_drain_gives_up_after_sustained_failure(),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
