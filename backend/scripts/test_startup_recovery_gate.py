"""Dedicated branch coverage for startup_recovery_gate.

The gate owns readiness only: a global fact plus per-session facts,
three state mutators (begin/done/failed), two readers, and two async
waiters. test_recovery_gate_any_loop covers the cross-loop happy paths;
this file closes the cold branches — early returns, no-ops, the
timeout-warning logs, and the failure-raise paths in both waiters.

Pure in-memory module: reset_for_tests() owns all global state, so no
isolated BETTER_AGENT_HOME is needed here.
"""

import asyncio
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import startup_recovery_gate as gate


def test_pending_and_session_pending_readers() -> None:
    gate.reset_for_tests()
    assert gate.is_pending() is False
    assert gate.is_session_pending("sid") is False

    gate.begin_recovery()
    assert gate.is_pending() is True
    assert gate.is_session_pending("sid") is False

    gate.register_session_recovery({"sid"})
    assert gate.is_session_pending("sid") is True
    gate.reset_for_tests()


def test_register_returns_early_when_not_pending() -> None:
    gate.reset_for_tests()
    assert gate.is_pending() is False
    gate.register_session_recovery({"sid"})
    # Not pending -> nothing registered.
    assert "sid" not in gate._session_ready
    gate.reset_for_tests()


def test_register_skips_falsy_session_ids() -> None:
    gate.reset_for_tests()
    gate.begin_recovery()
    gate.register_session_recovery({"", "real"})
    assert "real" in gate._session_ready
    assert "" not in gate._session_ready
    gate.reset_for_tests()


def test_mark_session_done_noop_for_unknown_session() -> None:
    gate.reset_for_tests()
    gate.begin_recovery()
    gate.register_session_recovery({"real"})
    # Popping a session that was never registered is a no-op.
    gate.mark_session_recovery_done("nope")
    assert "real" in gate._session_ready
    gate.reset_for_tests()


def test_failure_releases_registered_session_facts() -> None:
    gate.reset_for_tests()
    gate.begin_recovery()
    gate.register_session_recovery({"a", "b"})
    facts = dict(gate._session_ready)
    gate.mark_recovery_failed("disk gone")
    # _release_all set every per-session fact and cleared the registry.
    for fact in facts.values():
        assert fact.is_set()
    assert gate._session_ready == {}
    gate.reset_for_tests()


def test_global_wait_warns_on_timeout_while_pending() -> None:
    gate.reset_for_tests()
    gate.begin_recovery()

    async def body() -> None:
        await gate.wait_for_recovery_ready(timeout=0.05)

    asyncio.run(body())
    # Still pending: the waiter logged and continued without raising.
    assert gate.is_pending() is True
    gate.reset_for_tests()


def test_session_wait_unknown_session_falls_through_to_global_wait() -> None:
    gate.reset_for_tests()
    gate.begin_recovery()

    async def body() -> str:
        try:
            await gate.wait_for_session_recovery_ready("ghost", timeout=0.05)
        except RuntimeError as exc:
            return str(exc)
        return "ok"

    assert asyncio.run(body()) == "ok"
    gate.reset_for_tests()


def test_session_wait_unknown_session_raises_when_failed() -> None:
    gate.reset_for_tests()
    gate.begin_recovery()
    gate.mark_recovery_failed("boom")

    async def body() -> str:
        try:
            await gate.wait_for_session_recovery_ready("ghost", timeout=1.0)
        except RuntimeError as exc:
            return str(exc)
        return "no error"

    assert "boom" in asyncio.run(body())
    gate.reset_for_tests()


def test_session_wait_registered_session_warns_on_timeout() -> None:
    gate.reset_for_tests()
    gate.begin_recovery()
    gate.register_session_recovery({"sid"})

    async def body() -> str:
        try:
            await gate.wait_for_session_recovery_ready("sid", timeout=0.05)
        except RuntimeError as exc:
            return str(exc)
        return "ok"

    assert asyncio.run(body()) == "ok"
    gate.reset_for_tests()


def test_session_wait_registered_session_raises_when_failure_releases_it() -> None:
    """A registered session waiter is released by mark_recovery_failed's
    _release_all, wakes with the fact set, then sees _failed and raises."""
    gate.reset_for_tests()
    gate.begin_recovery()
    gate.register_session_recovery({"sid"})
    outcome: dict[str, str] = {}
    entered = threading.Event()

    def worker() -> None:
        async def body() -> str:
            entered.set()
            try:
                await gate.wait_for_session_recovery_ready("sid", timeout=5.0)
            except RuntimeError as exc:
                return str(exc)
            return "no error"

        outcome["value"] = asyncio.run(body())

    t = threading.Thread(target=worker)
    t.start()
    assert entered.wait(timeout=5.0)
    # Let the waiter register on the per-session fact before failing.
    threading.Event().wait(0.2)
    gate.mark_recovery_failed("disk gone")
    t.join(timeout=5.0)
    assert "disk gone" in outcome.get("value", "")
    gate.reset_for_tests()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("PASS")
