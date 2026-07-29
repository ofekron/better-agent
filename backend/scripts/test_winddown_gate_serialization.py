"""Wind-down gate contract (per-turn runner): a new --resume spawn on a
native session must serialize behind ANY registered run on that same
native session until its `released` event fires — a second CLI spawned
while the previous instance is still shutting down cross-process
ghost-enqueues the prompt.

Locks:
  T0  stale persisted provider authority fails before spawn
  T1  same native session + registered blocker → spawn deferred
  T2  blocker released → deferred spawn fires (event-driven, no poll)
  T3  fork=True is exempt (worker forks create a NEW native session)
  T4  different native session does not block
  T5  recovery stub (SimpleNamespace with `released`) participates:
      _cleanup_run fires its event and the deferred spawn proceeds
  T6  blocking persistence/spawn after release stays off the event loop

Run with:
    cd backend && PYTHONPATH=. .venv/bin/python scripts/test_winddown_gate_serialization.py
"""
from __future__ import annotations

import atexit
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
TEST_HOME = _test_home.TestHome.acquire("bc-test-winddown-gate-")
atexit.register(TEST_HOME.release)

import config_store  # noqa: E402
from execution_template import PreparedExecution, prepare_execution  # noqa: E402
from provider import start_prepared_run  # noqa: E402
from provider_claude import ClaudeProvider  # noqa: E402
from provider_execution_contract import provider_family_contract  # noqa: E402
from runs_dir import runs_root  # noqa: E402

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


def _mk_provider(
    *,
    spawn_delay: float = 0.0,
) -> tuple[ClaudeProvider, list[str], list[tuple[str, str, int]]]:
    record = config_store.add_provider({
        "name": "Wind-down gate fixture",
        "kind": "claude",
        "mode": "subscription",
        "runner": "native",
    })
    prov = ClaudeProvider(record)
    spawned: list[str] = []
    admitted: list[tuple[str, str, int]] = []

    def persist_and_start(_execution, *, arguments, loop, queue) -> None:
        del _execution, loop, queue
        runtime = prov.runtime_record()
        admitted.append((
            runtime["id"],
            runtime["generation"],
            runtime["revision"],
        ))
        if spawn_delay:
            time.sleep(spawn_delay)
        spawned.append(arguments["run_id"])

    prov._persist_and_start_execution = persist_and_start  # type: ignore[method-assign]
    return prov, spawned, admitted


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


def _prepare(
    prov: ClaudeProvider,
    *,
    run_id: str,
    session_id: str | None,
    fork: bool = False,
) -> PreparedExecution:
    record = prov.record
    return prepare_execution(
        record,
        provider_contract=provider_family_contract(
            record,
            payload={"test": "wind-down-gate-serialization"},
        ),
        run_id=run_id,
        prompt="hello",
        cwd="/tmp",
        model=None,
        reasoning_effort=None,
        session_id=session_id,
        mode="native",
        app_session_id="app-1",
        fork=fork,
    )


def _start(
    prov: ClaudeProvider,
    loop: asyncio.AbstractEventLoop,
    *,
    run_id: str,
    session_id: str | None,
    fork: bool = False,
) -> PreparedExecution:
    execution = _prepare(
        prov,
        run_id=run_id,
        session_id=session_id,
        fork=fork,
    )
    start_prepared_run(
        prov,
        execution,
        loop=loop,
        queue=asyncio.Queue(),
    )
    return execution


async def _main() -> None:
    loop = asyncio.get_running_loop()

    print("T0 stale persisted authority fails before spawn")
    prov, spawned, admitted = _mk_provider()
    execution = _prepare(
        prov,
        run_id="run-stale-authority",
        session_id="native-sid-stale",
    )
    changed = config_store.update_provider(
        prov.id,
        {"nickname": "changed after prepare"},
        expected_generation=prov.record["generation"],
        expected_revision=prov.record["revision"],
    )
    check(changed is not None, "provider authority revision advanced")
    rejected = False
    try:
        start_prepared_run(
            prov,
            execution,
            loop=loop,
            queue=asyncio.Queue(),
        )
    except config_store.ProviderConfigConflict:
        rejected = True
    check(rejected, "stale provider authority rejected")
    check(
        spawned == [] and admitted == [],
        "stale authority never reached persistence or spawn",
    )

    print("T1/T2 same-session blocker defers; released fires the spawn")
    prov, spawned, admitted = _mk_provider()
    blocker = _blocker("native-sid-1")
    prov._runs[blocker.run_id] = blocker
    execution = _start(prov, loop, run_id="run-new", session_id="native-sid-1")
    check(spawned == [], "spawn deferred while blocker is registered")
    check(
        not (runs_root() / "run-new").exists(),
        "deferred run has no pre-admission directory",
    )
    check(execution.admission_pending, "deferred admission remains pending")
    prov._cleanup_run(blocker.run_id)  # sets released + deregisters
    spawn_ready = await _wait_for(lambda: spawned == ["run-new"])
    check(spawn_ready, "deferred spawn became ready after release")
    check(spawned == ["run-new"], f"deferred spawn fired after release ({spawned})")
    check(
        spawn_ready and execution.wait_for_admission(),
        "deferred admission resolves after spawn",
    )
    check(
        admitted == [(
            prov.record["id"],
            prov.record["generation"],
            prov.record["revision"],
        )],
        "spawn consumed the admitted persisted provider authority",
    )

    print("T3 fork=True bypasses the gate")
    prov, spawned, _admitted = _mk_provider()
    blocker = _blocker("native-sid-2")
    prov._runs[blocker.run_id] = blocker
    _start(prov, loop, run_id="run-fork", session_id="native-sid-2", fork=True)
    check(spawned == ["run-fork"], "fork spawn not deferred")

    print("T4 different native session does not block")
    prov, spawned, _admitted = _mk_provider()
    blocker = _blocker("native-sid-3")
    prov._runs[blocker.run_id] = blocker
    _start(prov, loop, run_id="run-other", session_id="native-sid-OTHER")
    check(spawned == ["run-other"], "unrelated session spawns immediately")

    print("T5 recovery stub participates via _cleanup_run")
    prov, spawned, _admitted = _mk_provider()
    stub = _blocker("native-sid-4")
    prov._runs[stub.run_id] = stub
    _start(prov, loop, run_id="run-after-stub", session_id="native-sid-4")
    check(spawned == [], "spawn deferred behind recovery stub")
    prov._cleanup_run(stub.run_id)
    check(
        await _wait_for(lambda: spawned == ["run-after-stub"]),
        "recovery-stub release made deferred spawn ready",
    )
    check(stub.released.is_set(), "cleanup fired the stub's released event")
    check(spawned == ["run-after-stub"], "spawn proceeded after stub cleanup")

    print("T6 deferred re-entry does not block the event loop")
    # `_start_after_release` is a loop task, while persistence and spawn
    # can block synchronously. The delay proves deferred re-entry dispatches
    # that boundary off-loop.
    prov, spawned, _admitted = _mk_provider(spawn_delay=0.3)
    blocker = _blocker("native-sid-5")
    prov._runs[blocker.run_id] = blocker
    _start(prov, loop, run_id="run-heartbeat", session_id="native-sid-5")
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


async def _wait_for(pred, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


def main() -> int:
    try:
        asyncio.run(_main())
    finally:
        TEST_HOME.release()
    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        return 1
    print("OK: wind-down gate serialization")
    return 0


if __name__ == "__main__":
    sys.exit(main())
