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
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402
_test_home.isolate("bc-test-winddown-gate-")

from provider_claude import ClaudeProvider  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


def _mk_provider() -> tuple[ClaudeProvider, list[str]]:
    prov = ClaudeProvider({"id": "test-gate"})
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
    prov.start_run(
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
    _start(prov, loop, run_id="run-new", session_id="native-sid-1")
    await _drain()
    check(spawned == [], "spawn deferred while blocker is registered")
    prov._cleanup_run(blocker.run_id)  # sets released + deregisters
    await _drain()
    check(spawned == ["run-new"], f"deferred spawn fired after release ({spawned})")

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
