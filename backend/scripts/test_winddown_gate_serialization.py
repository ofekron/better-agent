"""Wind-down gate contract (per-turn runner): a new --resume spawn on a
native session must serialize behind ANY registered run on that same
native session until its `released` event fires — a second CLI spawned
while the previous instance is still shutting down cross-process
ghost-enqueues the prompt.

Locks:
  T1  same native session + registered blocker → spawn deferred
  T2  blocker released → deferred spawn fires (event-driven, no poll)
  T3  fork=True is exempt (worker forks create a NEW native session)
  T4  different native session does not block
  T5  recovery stub (SimpleNamespace with `released`) participates:
      _cleanup_run fires its event and the deferred spawn proceeds

Run with:
    cd backend && PYTHONPATH=. .venv/bin/python scripts/test_winddown_gate_serialization.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402
_test_home.isolate("bc-test-winddown-gate-")

from provider import prepare_and_start_run  # noqa: E402
from provider_claude import ClaudeProvider  # noqa: E402
from runs_dir import runs_root  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


def _mk_provider() -> tuple[ClaudeProvider, list[str]]:
    prov = ClaudeProvider({
        "id": "test-gate",
        "kind": "claude",
        "generation": "3c5c56ad-90ba-41b9-9f2d-aa4ab874ce83",
        "revision": 0,
    })
    spawned: list[str] = []
    prov._spawn_run = (  # type: ignore[method-assign]
        lambda **kw: spawned.append(kw["run_id"])
    )
    return prov, spawned


def _blocker(session_id: str) -> SimpleNamespace:
    """Registered run shape as the gate sees it — recovery stubs are
    SimpleNamespace, live runs are RunState; the gate reads session_id
    + released via getattr on both."""
    return SimpleNamespace(
        run_id="blocker-1",
        app_session_id="app-1",
        session_id=session_id,
        released=asyncio.Event(),
    )


def _start(prov: ClaudeProvider, loop, *, run_id: str, session_id, fork=False):
    return prepare_and_start_run(
        prov,
        run_id=run_id,
        prompt="hello",
        cwd="/tmp",
        loop=loop,
        queue=asyncio.Queue(),
        model=None,
        reasoning_effort=None,
        session_id=session_id,
        mode="native",
        app_session_id="app-1",
        fork=fork,
    )


async def _drain() -> None:
    # Let the scheduled gate task run to its await point / completion.
    for _ in range(20):
        await asyncio.sleep(0.01)


async def _main() -> None:
    loop = asyncio.get_running_loop()

    print("T1/T2 same-session blocker defers; released fires the spawn")
    prov, spawned = _mk_provider()
    blocker = _blocker("native-sid-1")
    prov._runs[blocker.run_id] = blocker
    execution = _start(prov, loop, run_id="run-new", session_id="native-sid-1")
    await _drain()
    check(spawned == [], "spawn deferred while blocker is registered")
    check(
        not (runs_root() / "run-new").exists(),
        "deferred run has no pre-admission directory",
    )
    check(execution.admission_pending, "deferred admission remains pending")
    prov._cleanup_run(blocker.run_id)  # sets released + deregisters
    await _drain()
    check(spawned == ["run-new"], f"deferred spawn fired after release ({spawned})")
    check(execution.wait_for_admission(), "deferred admission resolves after spawn")

    print("T3 fork=True bypasses the gate")
    prov, spawned = _mk_provider()
    blocker = _blocker("native-sid-2")
    prov._runs[blocker.run_id] = blocker
    _start(prov, loop, run_id="run-fork", session_id="native-sid-2", fork=True)
    check(spawned == ["run-fork"], "fork spawn not deferred")

    print("T4 different native session does not block")
    prov, spawned = _mk_provider()
    blocker = _blocker("native-sid-3")
    prov._runs[blocker.run_id] = blocker
    _start(prov, loop, run_id="run-other", session_id="native-sid-OTHER")
    check(spawned == ["run-other"], "unrelated session spawns immediately")

    print("T5 recovery stub participates via _cleanup_run")
    prov, spawned = _mk_provider()
    stub = _blocker("native-sid-4")
    prov._runs[stub.run_id] = stub
    _start(prov, loop, run_id="run-after-stub", session_id="native-sid-4")
    await _drain()
    check(spawned == [], "spawn deferred behind recovery stub")
    prov._cleanup_run(stub.run_id)
    await _drain()
    check(stub.released.is_set(), "cleanup fired the stub's released event")
    check(spawned == ["run-after-stub"], "spawn proceeded after stub cleanup")

    print("T6 deferred re-entry does not block the event loop")
    # Regression test for a real bug: `_start_after_release` runs as a
    # loop task, and used to call `self.start_run(...)` directly rather
    # than via `_to_turn_dispatch_thread` -- a `_spawn_run` that blocks
    # synchronously (as the MCP prewarm gate's bounded wait now can, up
    # to ~8-9.5s) would freeze the entire backend event loop for every
    # other session on that path. `_spawn_run` here does a real blocking
    # `time.sleep`, standing in for that synchronous wait, to prove the
    # fix actually routes it off-loop instead of just asserting it does.
    prov, spawned = _mk_provider()
    prov._spawn_run = (  # type: ignore[method-assign]
        lambda **kw: (time.sleep(0.3), spawned.append(kw["run_id"]))
    )
    blocker = _blocker("native-sid-5")
    prov._runs[blocker.run_id] = blocker
    _start(prov, loop, run_id="run-heartbeat", session_id="native-sid-5")
    await _drain()
    prov._cleanup_run(blocker.run_id)

    # Timer drift, not completion count, is what actually distinguishes
    # "ran concurrently" from "ran but was delayed": a blocked loop still
    # eventually fires every queued `asyncio.sleep`, just late. Compare
    # each heartbeat's fire time against its scheduled time; a blocked
    # loop stalls every tick queued during the 0.3s block, so max drift
    # spikes to ~0.3s, while an unblocked loop stays within a few ms.
    start = time.monotonic()
    max_drift = 0.0

    async def _heartbeat() -> None:
        nonlocal max_drift
        for i in range(1, 21):
            await asyncio.sleep(0.01)
            drift = (time.monotonic() - start) - (i * 0.01)
            max_drift = max(max_drift, drift)

    await asyncio.gather(_heartbeat(), _wait_for(lambda: spawned == ["run-heartbeat"]))
    check(
        max_drift < 0.15,
        f"loop stayed responsive (max heartbeat drift {max_drift:.3f}s) while "
        "the deferred blocking spawn ran off-thread",
    )
    check(spawned == ["run-heartbeat"], "deferred spawn still completed")


async def _wait_for(pred, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            return
        await asyncio.sleep(0.01)


def main() -> int:
    asyncio.run(_main())
    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        return 1
    print("OK: wind-down gate serialization")
    return 0


if __name__ == "__main__":
    sys.exit(main())
