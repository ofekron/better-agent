"""The gate must serve waiters that are NOT on the main loop.

provisioning.run_sync drives delegation via asyncio.run(...) on a private
loop in a worker thread, and that chain awaits wait_for_recovery_ready.
Before AsyncFact the gate held an asyncio.Event bound to the main loop
and fell back to polling a bool for those callers.
"""

import asyncio
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import startup_recovery_gate as gate


def test_foreign_loop_waiter_is_released_without_polling() -> None:
    gate.reset_for_tests()
    gate.begin_recovery()
    result = {}
    entered = threading.Event()

    def worker():
        # Exactly provisioning.run_sync's shape: a brand-new loop on its
        # own thread, no relationship to whoever marks recovery done.
        async def body():
            entered.set()
            await gate.wait_for_recovery_ready(timeout=5)
            return "released"

        result["value"] = asyncio.run(body())

    t = threading.Thread(target=worker)
    t.start()
    assert entered.wait(timeout=5)
    # Let the waiter register before the fact is set.
    threading.Event().wait(0.2)
    gate.mark_recovery_done()
    t.join(timeout=5)
    assert result.get("value") == "released"
    gate.reset_for_tests()


def test_per_session_wait_released_from_another_thread() -> None:
    gate.reset_for_tests()
    gate.begin_recovery()
    gate.register_session_recovery({"sid-a", "sid-b"})
    released = {}
    entered = threading.Event()

    def worker():
        async def body():
            entered.set()
            await gate.wait_for_session_recovery_ready("sid-a", timeout=5)
            return True

        released["a"] = asyncio.run(body())

    t = threading.Thread(target=worker)
    t.start()
    assert entered.wait(timeout=5)
    threading.Event().wait(0.2)
    gate.mark_session_recovery_done("sid-a")
    t.join(timeout=5)
    assert released.get("a") is True
    # The other session is still pending — releasing one must not release all.
    assert "sid-b" in gate._session_ready
    gate.reset_for_tests()


def test_failure_propagates_to_a_foreign_loop_waiter() -> None:
    gate.reset_for_tests()
    gate.begin_recovery()
    outcome = {}
    entered = threading.Event()

    def worker():
        async def body():
            entered.set()
            try:
                await gate.wait_for_recovery_ready(timeout=5)
            except RuntimeError as exc:
                return str(exc)
            return "no error"

        outcome["value"] = asyncio.run(body())

    t = threading.Thread(target=worker)
    t.start()
    assert entered.wait(timeout=5)
    threading.Event().wait(0.2)
    gate.mark_recovery_failed("disk gone")
    t.join(timeout=5)
    assert "disk gone" in outcome.get("value", "")
    gate.reset_for_tests()


def test_waiting_for_a_session_boosts_it() -> None:
    import recovery_schedule

    gate.reset_for_tests()
    gate.begin_recovery()
    gate.mark_recovery_done()  # not pending: wait returns promptly

    async def body():
        await gate.wait_for_session_recovery_ready("sid-open", timeout=1)

    asyncio.run(body())
    assert recovery_schedule.is_boosted("sid-open")
    gate.reset_for_tests()


def test_loop_affinity_workarounds_are_gone() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "startup_recovery_gate.py"
    ).read_text(encoding="utf-8")
    assert "_poll_recovery" not in source
    assert "_ready_bound_to_running_loop" not in source
    # The docstring still names asyncio.Event to explain why it is not
    # used; what must be gone is the import and any construction of one.
    assert "import asyncio" not in source
    assert "asyncio.Event()" not in source


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("PASS")
