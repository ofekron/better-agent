"""Wind-down gate regression tests.

Locks three behaviors of `ClaudeProvider.start_run`'s native-session
serialization gate:

1. A run parked behind a winding-down run reports as RUNNING. The turn
   drive-loop declares a run dead when the provider says it is not
   running; a parked run that reads as dead gets a synthesized
   "runner exited without delivering a complete event" failure, which is
   what makes a session look permanently uncontinuable.
2. Two runs released from the gate at the same instant spawn one after
   the other, never concurrently on the same native session.
3. A parked run whose turn was cancelled is dropped instead of spawning
   an orphan CLI into the session transcript.
"""

import asyncio
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths  # noqa: E402

_TEST_HOME = Path(tempfile.mkdtemp(prefix="ba-winddown-gate-"))
paths.engage_test_home(_TEST_HOME)

import provider_claude  # noqa: E402
from provider_claude import ClaudeProvider  # noqa: E402

NATIVE_SID = "native-session-under-test"
APP_SID = "app-session-under-test"


def _make_provider() -> ClaudeProvider:
    return ClaudeProvider({"id": "test-provider", "kind": "claude"})


class _FakePopen:
    def __init__(self) -> None:
        self.pid = 4242
        self._returncode = None

    def poll(self):
        return self._returncode

    def exit(self) -> None:
        self._returncode = 0


class _FakeRunState:
    """Stands in for the RunState `_spawn_run` would register."""

    def __init__(self, run_id: str, session_id: str) -> None:
        self.run_id = run_id
        self.session_id = session_id
        self.popen = _FakePopen()
        self.released = asyncio.Event()


def _spawn_kwargs(run_id: str, loop, queue) -> dict:
    return dict(
        run_id=run_id,
        prompt="hi",
        images=None,
        files=None,
        cwd="/tmp",
        loop=loop,
        queue=queue,
        model=None,
        reasoning_effort=None,
        session_id=NATIVE_SID,
        mode="native",
        app_session_id=APP_SID,
        source=None,
        disallowed_tools=None,
        setting_sources=None,
        backend_url=None,
        internal_token=None,
        fork=False,
        supervised=False,
        supervisor_agent_session_id=None,
        worker_agent_session_id=None,
        mssg_sender_session_id=None,
        is_worker=False,
        browser_harness_enabled=False,
        user_facing=True,
        working_mode=None,
        extra_env=None,
        continuation_chain=None,
        provider_run_config=None,
        capability_contexts=None,
        target_message_id=None,
        resolved_harness_run_config=None,
        turn_run_id=None,
        disabled_builtin_extensions=None,
        provisioned_tool_profile="",
    )


def _install_spawn_recorder(prov: ClaudeProvider, record: list, *, hold: float = 0.0):
    """Replace `_spawn_run` with a recorder that registers the run the
    way the real spawn does, so the gate sees it as a live blocker."""
    concurrent = {"max": 0, "now": 0}
    guard = threading.Lock()

    def fake_spawn(**kwargs):
        run_id = kwargs["run_id"]
        with guard:
            concurrent["now"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["now"])
        record.append(run_id)
        # The real `_spawn_run` builds the input payload, writes
        # input.json, and launches the process BEFORE registering the
        # run — that gap is the window in which an unsynchronized second
        # caller sees no blocker and spawns a second CLI on the same
        # native session.
        if hold:
            import time
            time.sleep(hold)
        prov._runs[run_id] = _FakeRunState(run_id, kwargs.get("session_id"))
        with guard:
            concurrent["now"] -= 1

    prov._spawn_run = fake_spawn  # type: ignore[assignment]
    return concurrent


async def test_parked_run_reports_running() -> None:
    prov = _make_provider()
    loop = asyncio.get_running_loop()
    spawned: list = []
    _install_spawn_recorder(prov, spawned)

    blocker = _FakeRunState("blocker0", NATIVE_SID)
    prov._runs["blocker0"] = blocker

    queue: asyncio.Queue = asyncio.Queue()
    await asyncio.to_thread(
        prov.start_run, **_spawn_kwargs("parked-1", loop, queue)
    )

    assert spawned == [], f"parked run must not spawn yet, got {spawned}"
    assert prov.is_running("parked-1") is True, (
        "a parked run must report as running — otherwise the turn "
        "drive-loop synthesizes a dead-runner failure"
    )
    assert await prov.is_running_off_loop("parked-1") is True

    # Releasing the blocker lets it through.
    prov._cleanup_run("blocker0")
    for _ in range(200):
        await asyncio.sleep(0.01)
        if spawned:
            break
    assert spawned == ["parked-1"], f"expected spawn after release, got {spawned}"
    assert prov.is_running("parked-1") is True
    assert not prov._parked_runs, "run must be unparked once spawned"
    print("PASS parked run reports running")


async def test_simultaneous_release_does_not_double_spawn() -> None:
    prov = _make_provider()
    loop = asyncio.get_running_loop()
    spawned: list = []
    # Hold inside spawn so a concurrent second spawn would overlap.
    concurrent = _install_spawn_recorder(prov, spawned, hold=0.25)

    blocker = _FakeRunState("blocker0", NATIVE_SID)
    prov._runs["blocker0"] = blocker

    queue: asyncio.Queue = asyncio.Queue()
    await asyncio.gather(
        asyncio.to_thread(
            prov.start_run, **_spawn_kwargs("parked-a", loop, queue)
        ),
        asyncio.to_thread(
            prov.start_run, **_spawn_kwargs("parked-b", loop, queue)
        ),
    )
    assert spawned == [], "both runs must park behind the live blocker"

    prov._cleanup_run("blocker0")
    # Let both gate waiters resolve and any spawn overlap materialize.
    await asyncio.sleep(1.5)

    assert concurrent["max"] <= 1, (
        "two CLIs were spawned concurrently on the same native session "
        f"(max concurrent spawns={concurrent['max']})"
    )
    assert len(spawned) == 1, (
        "only the first released run may spawn; the second must re-park "
        f"behind it, got {spawned}"
    )
    print("PASS simultaneous release serializes")


async def test_cancelled_parked_run_is_dropped() -> None:
    prov = _make_provider()
    loop = asyncio.get_running_loop()
    spawned: list = []
    _install_spawn_recorder(prov, spawned)

    blocker = _FakeRunState("blocker0", NATIVE_SID)
    prov._runs["blocker0"] = blocker

    queue: asyncio.Queue = asyncio.Queue()
    await asyncio.to_thread(
        prov.start_run, **_spawn_kwargs("parked-x", loop, queue)
    )
    assert prov.is_running("parked-x") is True

    assert prov.cancel_run("parked-x") is True
    assert prov.is_running("parked-x") is False

    prov._cleanup_run("blocker0")
    await asyncio.sleep(0.3)
    assert spawned == [], (
        "a parked run cancelled with its turn must never spawn — it would "
        f"orphan a CLI on the native session, got {spawned}"
    )
    print("PASS cancelled parked run dropped")


async def main() -> None:
    try:
        await test_parked_run_reports_running()
        await test_simultaneous_release_does_not_double_spawn()
        await test_cancelled_parked_run_is_dropped()
        print("ALL WIND-DOWN GATE TESTS PASSED")
    finally:
        shutil.rmtree(_TEST_HOME, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
