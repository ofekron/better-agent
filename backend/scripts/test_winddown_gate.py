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

import pytest

pytestmark = pytest.mark.anyio

import asyncio
import shutil
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths  # noqa: E402

_TEST_HOME = Path(tempfile.mkdtemp(prefix="ba-winddown-gate-"))
paths.engage_test_home(_TEST_HOME)

import provider_claude  # noqa: E402
from execution_template import prepare_execution  # noqa: E402
from provider_claude import ClaudeProvider  # noqa: E402

NATIVE_SID = "native-session-under-test"
APP_SID = "app-session-under-test"
PROVIDER_GENERATION = "0aee8bee-e4c9-4e2a-8d66-84eb6ad1d0cc"


def _make_provider() -> ClaudeProvider:
    record = {
        "id": "test-provider",
        "kind": "claude",
        "generation": PROVIDER_GENERATION,
        "revision": 1,
        "execution_revision": 1,
    }
    provider = ClaudeProvider(record)

    @contextmanager
    def authority_context(_execution, _start_arguments):
        yield record

    def persist_and_start(execution, *, arguments, loop, queue):
        provider._spawn_run(
            execution=execution,
            loop=loop,
            queue=queue,
            internal_token=arguments["internal_token"],
            extra_env=arguments["extra_env"],
        )

    provider._execution_authority_context = authority_context  # type: ignore[method-assign]
    provider.require_runtime_credential = lambda: None  # type: ignore[method-assign]
    provider._persist_and_start_execution = persist_and_start  # type: ignore[method-assign]
    return provider


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

    def __init__(self, run_id: str, session_id: str, turn_run_id=None) -> None:
        self.run_id = run_id
        self.session_id = session_id
        self.turn_run_id = turn_run_id
        self.popen = _FakePopen()
        self.released = asyncio.Event()
        self.tailer = None
        self.tailer_task = None


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


def _start_prepared(prov: ClaudeProvider, **kwargs) -> bool:
    loop = kwargs.pop("loop")
    queue = kwargs.pop("queue")
    execution = prepare_execution(prov.record, **kwargs)
    return prov.start_run(execution=execution, loop=loop, queue=queue)


def _install_spawn_recorder(prov: ClaudeProvider, record: list, *, hold: float = 0.0):
    """Replace `_spawn_run` with a recorder that registers the run the
    way the real spawn does, so the gate sees it as a live blocker."""
    concurrent = {"max": 0, "now": 0}
    guard = threading.Lock()

    def fake_spawn(*, execution, **_kwargs):
        arguments = execution.start_arguments()
        run_id = arguments["run_id"]
        with guard:
            concurrent["now"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["now"])
        record.append(run_id)
        # The real `_spawn_run` materializes the attested execution and
        # launches the process before registering the run. The gate must
        # serialize that entire interval.
        if hold:
            import time
            time.sleep(hold)
        prov._runs[run_id] = _FakeRunState(run_id, arguments["session_id"])
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
        _start_prepared, prov, **_spawn_kwargs("parked-1", loop, queue)
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
            _start_prepared, prov, **_spawn_kwargs("parked-a", loop, queue)
        ),
        asyncio.to_thread(
            _start_prepared, prov, **_spawn_kwargs("parked-b", loop, queue)
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
        _start_prepared, prov, **_spawn_kwargs("parked-x", loop, queue)
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


async def test_cancel_turn_reaches_parked_run() -> None:
    """Stop must end a turn that is still parked at the gate.

    The cancel fans out by the orchestrator's turn_run_id, which never
    matches the provider's own run id, and a parked run has no run dir
    for the sentinel fallback to touch — so the turn used to stay wedged
    with no way out."""
    prov = _make_provider()
    loop = asyncio.get_running_loop()
    spawned: list = []
    _install_spawn_recorder(prov, spawned)

    blocker = _FakeRunState("blocker0", NATIVE_SID)
    prov._runs["blocker0"] = blocker

    queue: asyncio.Queue = asyncio.Queue()
    kwargs = _spawn_kwargs("parked-c", loop, queue)
    kwargs["turn_run_id"] = "turn-run-under-test"
    await asyncio.to_thread(_start_prepared, prov, **kwargs)
    assert prov.is_running("parked-c") is True

    assert prov.cancel_turn("turn-run-under-test") is True, (
        "cancel_turn must resolve a parked run by turn_run_id — otherwise "
        "the Stop button is a no-op for a turn stuck at the gate"
    )
    assert prov.is_running("parked-c") is False

    prov._cleanup_run("blocker0")
    await asyncio.sleep(0.3)
    assert spawned == [], (
        f"a cancelled parked run must never spawn, got {spawned}"
    )
    print("PASS cancel_turn reaches parked run")


async def test_hung_runner_releases_gate() -> None:
    """A runner that finalized its turn but never exits must not wedge
    the native session forever.

    Production case: the runner logged `runner done`, wrote complete.json,
    then blocked in interpreter shutdown joining a non-daemon executor
    thread. It stayed registered, so every later turn on that native
    session parked at the gate until the backend restarted."""
    prov = _make_provider()
    loop = asyncio.get_running_loop()
    spawned: list = []
    _install_spawn_recorder(prov, spawned)

    hung = _FakeRunState("hung0", NATIVE_SID)
    prov._runs["hung0"] = hung

    reaped: dict = {}

    class _StubControl:
        def terminate_tree(self, popen, *, timeout=3.0):
            reaped["pid"] = popen.pid
            popen.exit()
            return True

    orig_control = provider_claude._process_control
    orig_grace = provider_claude._WIND_DOWN_GRACE
    provider_claude._process_control = lambda: _StubControl()
    provider_claude._WIND_DOWN_GRACE = 0.3

    async def _no_drain(rs):
        return None

    prov._await_tailer_drained = _no_drain  # type: ignore[assignment]

    try:
        queue: asyncio.Queue = asyncio.Queue()
        await asyncio.to_thread(
            _start_prepared, prov, **_spawn_kwargs("after-hung", loop, queue)
        )
        assert spawned == [], "the new turn must park behind the hung run"

        # The wind-down epilogue for a runner whose process never exits.
        # Unbounded before the fix — wait_for turns that hang into a
        # clean failure.
        await asyncio.wait_for(prov._watch_process_exit(hung), timeout=10)

        assert reaped.get("pid") == hung.popen.pid, (
            "the hung runner was never reaped, so the gate never released"
        )
        for _ in range(200):
            await asyncio.sleep(0.01)
            if spawned:
                break
        assert spawned == ["after-hung"], (
            "the parked turn must spawn once the hung runner is reaped, "
            f"got {spawned}"
        )
    finally:
        provider_claude._process_control = orig_control
        provider_claude._WIND_DOWN_GRACE = orig_grace
    print("PASS hung runner releases gate")


async def test_cancel_turn_drops_every_parked_attempt() -> None:
    """Retry/continuation attempts mint a new provider run id per attempt
    under one turn_run_id. Cancelling only the first leaves the survivor
    blocking the gate for every later turn on the session."""
    prov = _make_provider()
    loop = asyncio.get_running_loop()
    spawned: list = []
    _install_spawn_recorder(prov, spawned)
    prov._runs["blocker0"] = _FakeRunState("blocker0", NATIVE_SID)

    for run_id in ("attempt-1", "attempt-2"):
        kwargs = _spawn_kwargs(run_id, loop, asyncio.Queue())
        kwargs["turn_run_id"] = "shared-turn"
        await asyncio.to_thread(_start_prepared, prov, **kwargs)
    assert len(prov._parked_runs) == 2

    assert prov.cancel_turn("shared-turn") is True
    assert not prov._parked_runs, (
        "every parked attempt of the cancelled turn must be dropped, "
        f"still parked: {list(prov._parked_runs)}"
    )

    prov._cleanup_run("blocker0")
    await asyncio.sleep(0.3)
    assert spawned == [], f"no cancelled attempt may spawn, got {spawned}"
    print("PASS cancel_turn drops every parked attempt")


async def test_recovered_run_is_never_signalled() -> None:
    """A recovered run's `popen` is a stub around a pid read off disk,
    with no ownership proof. After a restart that pid may belong to an
    unrelated process, so the reap must never signal it — while the gate
    must still release."""
    prov = _make_provider()
    loop = asyncio.get_running_loop()
    spawned: list = []
    _install_spawn_recorder(prov, spawned)

    recovered = _FakeRunState("recovered0", NATIVE_SID)
    recovered.popen.recovered_stub = True  # never exits, never verifiable
    prov._runs["recovered0"] = recovered

    signalled: list = []

    class _StubControl:
        def terminate_tree(self, popen, *, timeout=3.0):
            signalled.append(popen.pid)
            popen.exit()
            return True

    orig_control = provider_claude._process_control
    orig_grace = provider_claude._WIND_DOWN_GRACE
    provider_claude._process_control = lambda: _StubControl()
    provider_claude._WIND_DOWN_GRACE = 0.3

    async def _no_drain(rs):
        return None

    prov._await_tailer_drained = _no_drain  # type: ignore[assignment]

    try:
        await asyncio.to_thread(
            _start_prepared,
            prov,
            **_spawn_kwargs("after-recovered", loop, asyncio.Queue()),
        )
        await asyncio.wait_for(prov._watch_process_exit(recovered), timeout=10)

        assert signalled == [], (
            "the backend signalled an unverified recovered pid — a "
            f"recycled pid would kill an unrelated process group: {signalled}"
        )
        for _ in range(200):
            await asyncio.sleep(0.01)
            if spawned:
                break
        assert spawned == ["after-recovered"], (
            f"the gate must still release for a recovered run, got {spawned}"
        )
    finally:
        provider_claude._process_control = orig_control
        provider_claude._WIND_DOWN_GRACE = orig_grace
    print("PASS recovered run released without being signalled")


async def main() -> None:
    try:
        await test_parked_run_reports_running()
        await test_simultaneous_release_does_not_double_spawn()
        await test_cancelled_parked_run_is_dropped()
        await test_cancel_turn_reaches_parked_run()
        await test_hung_runner_releases_gate()
        await test_cancel_turn_drops_every_parked_attempt()
        await test_recovered_run_is_never_signalled()
        print("ALL WIND-DOWN GATE TESTS PASSED")
    finally:
        shutil.rmtree(_TEST_HOME, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
