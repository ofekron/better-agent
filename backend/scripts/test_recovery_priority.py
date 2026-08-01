from __future__ import annotations

import asyncio
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import recovery_priority as rp  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state():
    rp.reset_for_tests()
    yield
    rp.reset_for_tests()


def test_count_starts_zero():
    assert rp.interactive_request_count() == 0


def test_started_increments_and_finished_decrements():
    rp.interactive_request_started()
    rp.interactive_request_started()
    assert rp.interactive_request_count() == 2
    rp.interactive_request_finished()
    assert rp.interactive_request_count() == 1


def test_finished_never_goes_negative():
    rp.interactive_request_started()
    rp.interactive_request_finished()
    rp.interactive_request_finished()  # one extra finish
    assert rp.interactive_request_count() == 0


def test_admit_returns_immediately_when_no_interactive():
    async def _run():
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await rp.admit_recovery_quantum()
        return loop.time() - t0

    elapsed = asyncio.run(_run())
    # count == 0 → early return, no wait
    assert elapsed < rp.MAX_INTERACTIVE_DEFER_SECONDS


# --- _rebind_quiet_to_running_loop contract ----------------------------------

def test_rebind_is_noop_without_running_loop():
    # Called from a sync context (no event loop) → must return without touching _quiet.
    rp._quiet_loop = object()  # force "different loop" so the loop check is the only guard
    before = rp._quiet
    rp._rebind_quiet_to_running_loop()
    assert rp._quiet is before


def test_rebind_preserves_set_state_across_loop_change():
    async def _run():
        rp._quiet_loop = object()  # pretend bound to a different (dead) loop
        rp._quiet.set()
        rp._rebind_quiet_to_running_loop()
        assert rp._quiet.is_set() is True
        assert rp._quiet_loop is asyncio.get_running_loop()

    asyncio.run(_run())


def test_rebind_preserves_cleared_state_across_loop_change():
    async def _run():
        rp._quiet_loop = object()
        rp._quiet.clear()
        rp._rebind_quiet_to_running_loop()
        assert rp._quiet.is_set() is False
        assert rp._quiet_loop is asyncio.get_running_loop()

    asyncio.run(_run())


def test_rebind_is_noop_when_loop_unchanged():
    async def _run():
        rp._rebind_quiet_to_running_loop()  # binds to this loop
        bound = rp._quiet
        rp._rebind_quiet_to_running_loop()  # same loop → no recreation
        assert rp._quiet is bound

    asyncio.run(_run())


def test_admit_waits_then_succeeds_and_escapes_starvation(monkeypatch):
    # module-level asyncio.Event binds to its first event loop, so both wait
    # paths must run inside ONE loop to avoid cross-loop detachment.
    monkeypatch.setattr(rp, "MAX_INTERACTIVE_DEFER_SECONDS", 0.05)

    async def _run():
        # success path: an interactive request finishes mid-wait → _quiet set
        rp.interactive_request_started()

        async def _finish():
            await asyncio.sleep(0.005)
            rp.interactive_request_finished()

        await asyncio.gather(rp.admit_recovery_quantum(), _finish())
        assert rp.interactive_request_count() == 0

        # starvation-escape path: a request that never finishes → wait_for times out
        rp.interactive_request_started()
        await rp.admit_recovery_quantum()
        rp.interactive_request_finished()  # cleanup

    asyncio.run(_run())
