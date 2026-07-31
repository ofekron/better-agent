"""The awaiting-provisioned-fork fact: transitions, waiter wakeup, broadcast.

A file-edit session is returned to the client before its provisioned base
is warm. Until the warm binds `forked_from_agent_sid`, dispatching a turn
would start an UNFORKED provider session (turn_manager reads that field as
the only fork signal), so the fact gates prompt dispatch.

Run with:
    cd backend && .venv/bin/python scripts/test_session_readiness_gate.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import _test_home
TMP_HOME = Path(_test_home.isolate("bc-test-readiness-gate-"))

import session_readiness  # noqa: E402
import session_store  # noqa: E402
import session_ws_broadcaster  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def check(cond: bool, label: str) -> None:
    if not cond:
        raise AssertionError(label)
    print(f"  ok: {label}")


def _new_session(pending: bool) -> str:
    session = session_manager.create(
        name="file-edit-test",
        cwd=str(TMP_HOME),
        orchestration_mode="native",
        source="web",
        user_initiated=True,
        fork_provisioning=session_readiness.pending_fact() if pending else None,
    )
    return session["id"]


def test_presence_is_the_gate() -> None:
    gated = _new_session(pending=True)
    check(
        session_readiness.is_awaiting_fork_provisioning(gated),
        "a session created with the fact is gated",
    )
    ungated = _new_session(pending=False)
    check(
        not session_readiness.is_awaiting_fork_provisioning(ungated),
        "a session created without the fact is NOT gated (ordinary sessions unaffected)",
    )
    check(
        not session_readiness.fork_provisioning_failed(gated),
        "pending is not failed",
    )


def test_fact_lands_in_the_first_write() -> None:
    """Fail-closed: the fact must be part of the record create_session
    itself persists. Stamping it afterwards would leave a crash window in
    which the session comes back dispatchable-but-unbound."""
    record = session_store.create_session(
        cwd=str(TMP_HOME),
        orchestration_mode="native",
        fork_provisioning=session_readiness.pending_fact(),
    )
    check(
        (record.get("fork_provisioning") or {}).get("state") == "pending",
        "create_session builds the fact into the record it persists",
    )
    reloaded = session_manager.get_fields(record["id"], ("fork_provisioning",))
    check(
        (reloaded.get("fork_provisioning") or {}).get("state") == "pending",
        "the fact survives a store round-trip",
    )


async def _assert_blocks_then(sid: str) -> asyncio.Task:
    task = asyncio.create_task(session_readiness.wait_until_resolved(sid))
    for _ in range(3):
        await asyncio.sleep(0)
    check(not task.done(), "a waiter blocks while the binding is pending")
    return task


async def test_waiter_wakes_on_ready() -> None:
    sid = _new_session(pending=True)
    task = await _assert_blocks_then(sid)
    await session_readiness.mark_ready(sid)
    outcome = await asyncio.wait_for(task, timeout=5)
    check(outcome == session_readiness.RESOLVED_READY, "waiter resolves as ready")
    check(
        not session_readiness.is_awaiting_fork_provisioning(sid),
        "clearing removes the fact so the session is no longer gated",
    )
    check(
        session_manager.get_fields(sid, ("fork_provisioning",)).get(
            "fork_provisioning"
        )
        is None,
        "the fact is REMOVED, not left behind as a stale pending marker",
    )


async def test_waiter_wakes_on_failure() -> None:
    sid = _new_session(pending=True)
    task = await _assert_blocks_then(sid)
    await session_readiness.mark_failed(sid, RuntimeError("provider CLI never returned a sid"))
    outcome = await asyncio.wait_for(task, timeout=5)
    check(
        outcome == session_readiness.RESOLVED_FAILED,
        "waiter resolves as failed (so queued work fails loudly, never runs unforked)",
    )
    check(session_readiness.fork_provisioning_failed(sid), "the session reports failed")
    check(
        not session_readiness.is_awaiting_fork_provisioning(sid),
        "failed is not pending — the gate must not park forever",
    )
    stored = session_manager.get_fields(sid, ("fork_provisioning",))["fork_provisioning"]
    check(
        "RuntimeError" in stored["error"] and "never returned a sid" in stored["error"],
        "the failure reason is preserved for the UI",
    )


async def test_resolution_before_wait_is_not_missed() -> None:
    """No lost wakeup: a resolution that lands before the waiter starts
    must be observed from the durable record, not awaited forever."""
    sid = _new_session(pending=True)
    await session_readiness.mark_ready(sid)
    outcome = await asyncio.wait_for(
        session_readiness.wait_until_resolved(sid), timeout=5
    )
    check(outcome == session_readiness.RESOLVED_READY, "already-resolved wait returns immediately")


def test_error_is_sanitized() -> None:
    long_error = "x" * 5000
    sanitized = session_readiness._sanitize_error(RuntimeError(long_error))
    check(len(sanitized) <= 220, "an oversized provider error is truncated before persisting")
    check(
        session_readiness._sanitize_error("a\n  b\tc") == "a b c",
        "whitespace/newlines are collapsed so the reason stays one readable line",
    )


def test_change_kind_is_broadcast() -> None:
    """Regression lock: a change kind missing from _METADATA_KINDS is
    dropped with a one-shot warning, so the client would never learn the
    session stopped provisioning."""
    check(
        "fork_provisioning_changed" in session_ws_broadcaster._METADATA_KINDS,
        "the fork_provisioning change kind is registered for broadcast",
    )


def main_run() -> int:
    try:
        test_presence_is_the_gate()
        test_fact_lands_in_the_first_write()
        asyncio.run(test_waiter_wakes_on_ready())
        asyncio.run(test_waiter_wakes_on_failure())
        asyncio.run(test_resolution_before_wait_is_not_missed())
        test_error_is_sanitized()
        test_change_kind_is_broadcast()
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)
    print("PASS session readiness gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_run())
