"""Replay-only buckets integrate on the recovery thread, but some of
that path reaches loop-bound engines.

`lifecycle_command_engine` and the lifecycle state tree assert they are
called on the loop they were bound to, and raise otherwise. The terminal
emit on the DEAD-run path touches both `lifecycle_commands` and
`coordinator.user_prompt_manager`, so it must hop back to the main loop
even though its caller runs on the recovery thread.

This regression-locks that hop: the emitter must execute on the main
loop no matter which loop invokes it.
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths

paths.engage_test_home(tempfile.mkdtemp(prefix="offloop-lifecycle-"))

import loop_affinity  # noqa: E402
import provider  # noqa: E402
import recovery_manager  # noqa: E402
import run_recovery  # noqa: E402


def test_terminal_emit_runs_on_main_even_when_called_off_loop() -> None:
    ran_on: list[object] = []
    original = run_recovery._emit_recovered_user_message_terminal_on_main

    async def spy(*a, **kw):
        ran_on.append(asyncio.get_running_loop())

    run_recovery._emit_recovered_user_message_terminal_on_main = spy
    manager = recovery_manager.RecoveryManager()
    manager.start()
    try:
        async def scenario():
            main_loop = asyncio.get_running_loop()
            loop_affinity.bind_main_loop(main_loop)

            async def work():
                # Called from the recovery loop, exactly as a
                # replay-only bucket's integration would.
                assert asyncio.get_running_loop() is manager.loop
                await run_recovery._emit_recovered_user_message_terminal(
                    coordinator=None,
                    app_session_id="sid",
                    persist_sid="sid",
                    mode="manager",
                    agent_sid=None,
                    run_id="run-1",
                    cancelled=False,
                    assistant_msg={},
                    execution_turn_id=None,
                )

            await manager.run(work)
            assert ran_on == [main_loop], ran_on

        asyncio.run(scenario())
    finally:
        manager.stop()
        loop_affinity.reset_for_tests()
        run_recovery._emit_recovered_user_message_terminal_on_main = original


def test_recovered_retry_runs_on_main_even_when_requested_off_loop() -> None:
    ran_on: list[object] = []
    original = run_recovery._retry_recovered_run_on_main

    async def spy(**_kwargs):
        ran_on.append(asyncio.get_running_loop())

    run_recovery._retry_recovered_run_on_main = spy
    manager = recovery_manager.RecoveryManager()
    manager.start()
    try:
        async def scenario():
            main_loop = asyncio.get_running_loop()
            loop_affinity.bind_main_loop(main_loop)

            async def work():
                assert asyncio.get_running_loop() is manager.loop
                await run_recovery._retry_recovered_run(
                    coordinator=None,
                    provider=None,
                    desc={},
                    run_dir=Path("unused"),
                    app_sid="sid",
                    persist_sid="sid",
                    msg_id="msg",
                    recovering_msg_id=None,
                )

            await manager.run(work)
            assert ran_on == [main_loop], ran_on

        asyncio.run(scenario())
    finally:
        manager.stop()
        loop_affinity.reset_for_tests()
        run_recovery._retry_recovered_run_on_main = original


def test_loop_bound_engine_would_reject_the_off_loop_call() -> None:
    """Shows the failure this guards against is real, not theoretical."""

    class Engine:
        def __init__(self):
            self._loop = None

        def bind(self):
            self._loop = asyncio.get_running_loop()

        def snapshot(self):
            if self._loop is None or self._loop is not asyncio.get_running_loop():
                raise RuntimeError(
                    "lifecycle command engine is not bound to this loop",
                )
            return "ok"

    engine = Engine()
    manager = recovery_manager.RecoveryManager()
    manager.start()
    try:
        async def scenario():
            engine.bind()

            async def direct():
                return engine.snapshot()

            try:
                await manager.run(direct)
            except RuntimeError as exc:
                assert "not bound to this loop" in str(exc)
            else:
                raise AssertionError(
                    "expected the loop-bound engine to reject an off-loop call",
                )

            # Hopping back to main is what makes it work.
            loop_affinity.bind_main_loop(asyncio.get_running_loop())

            async def hopped():
                return await loop_affinity.call_on_main(direct)

            assert await manager.run(hopped) == "ok"

        asyncio.run(scenario())
    finally:
        manager.stop()
        loop_affinity.reset_for_tests()


def test_call_on_main_returns_values_and_propagates_errors() -> None:
    manager = recovery_manager.RecoveryManager()
    manager.start()
    try:
        async def scenario():
            loop_affinity.bind_main_loop(asyncio.get_running_loop())

            async def value():
                return 42

            async def boom():
                raise ValueError("nope")

            assert await manager.run(
                lambda: loop_affinity.call_on_main(value),
            ) == 42

            try:
                await manager.run(lambda: loop_affinity.call_on_main(boom))
            except ValueError as exc:
                assert str(exc) == "nope"
            else:
                raise AssertionError("error did not propagate across loops")

        asyncio.run(scenario())
    finally:
        manager.stop()
        loop_affinity.reset_for_tests()


def test_integration_reports_each_session_without_losing_sibling_progress() -> None:
    original_locked = run_recovery._integrate_one_locked
    original_root = run_recovery.session_manager._root_id_for
    original_get_provider = provider.get_provider
    seen: list[str] = []

    class FakeProvider:
        defunct = False

    async def integrate_locked(_coordinator, _provider, desc, **_kwargs):
        session_id = str(desc["app_session_id"])
        seen.append(session_id)
        if session_id == "failed":
            raise RuntimeError("integration failed")
        return run_recovery.RecoveryIntegrationOutcome.SUCCESS

    run_recovery._integrate_one_locked = integrate_locked
    run_recovery.session_manager._root_id_for = lambda sid: sid
    provider.get_provider = lambda *_args, **_kwargs: FakeProvider()
    try:
        async def scenario():
            outcomes = await run_recovery.integrate_recovered_runs(
                None,
                [
                    {
                        "run_id": "run-failed",
                        "app_session_id": "failed",
                        "provider_id": "provider",
                        "alive": True,
                    },
                    {
                        "run_id": "run-success",
                        "app_session_id": "success",
                        "provider_id": "provider",
                        "alive": True,
                    },
                ],
            )
            assert outcomes == {
                "failed": run_recovery.RecoveryIntegrationOutcome.RETRYABLE,
                "success": run_recovery.RecoveryIntegrationOutcome.SUCCESS,
            }
            assert sorted(seen) == ["failed", "success"]

        asyncio.run(scenario())
    finally:
        provider.get_provider = original_get_provider
        run_recovery.session_manager._root_id_for = original_root
        run_recovery._integrate_one_locked = original_locked


def test_integrate_one_propagates_leaf_outcome() -> None:
    original_locked = run_recovery._integrate_one_locked
    original_root = run_recovery.session_manager._root_id_for
    retryable = object()

    async def integrate_locked(*_args, **_kwargs):
        return retryable

    run_recovery._integrate_one_locked = integrate_locked
    run_recovery.session_manager._root_id_for = lambda _sid: "root"
    try:
        assert asyncio.run(run_recovery._integrate_one(
            None,
            None,
            {"app_session_id": "sid"},
        )) is retryable
    finally:
        run_recovery.session_manager._root_id_for = original_root
        run_recovery._integrate_one_locked = original_locked


def test_dead_run_branches_reach_the_terminal_emit() -> None:
    """The two call sites are gated by `not (alive and not has_complete)`
    — i.e. they fire for exactly the buckets routed off-main."""
    source = (
        Path(__file__).resolve().parents[1] / "run_recovery.py"
    ).read_text(encoding="utf-8")
    first = source.index("await _emit_recovered_user_message_terminal(")
    window = source[max(0, first - 400):first]
    assert "not (alive and not has_complete)" in window


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("PASS")
