"""Regression tests: a turn whose message persist silently no-ops must
abort loudly with zero bookkeeping — never run against a phantom target.

Incident: run 15ffd491 / session e0fd7b0d — `append_assistant_msg`
silently returned None (session lookup miss), turn_manager registered
the never-appended msg as the turn target anyway, the provider streamed
into KeyError storms, the prompt reported "not executed", and restart
recovery dropped the run ("target message not found"). The user prompt
was ALSO lost one call earlier with a false persisted-ack
(`persisted_user_msg or user_msg` mask).

Locks:
  1. strict/lenient contract on append_user_msg / append_assistant_msg.
  2. run_turn with an unresolvable persist_to aborts BEFORE any turn
     registration (KeyError propagates; no active_run_ids / _run_state /
     cancel_events / current_turn_workers leak; no user_message_persisted
     frame; provider layer never reached).
  3. Mid-turn variant: root vanishes between the user-msg persist and the
     assistant append — abort routes through the turn-failure path and the
     finally restores ALL bookkeeping (no phantom target left registered).

Run with:
    cd backend && .venv/bin/python scripts/test_turn_persist_strict.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402
_test_home.isolate("bc-test-turn-persist-strict-")

from session_manager import manager as session_manager  # noqa: E402
import session_store  # noqa: E402
import pytest  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"

_COORD = None


def _coordinator():
    global _COORD
    if _COORD is None:
        import orchestrator
        _COORD = orchestrator.Coordinator()
    return _COORD


def test_append_contracts() -> None:
    """Lenient append on a missing sid returns None; strict raises
    KeyError naming the miss branch."""
    missing = "00000000-dead-beef-0000-000000000000"

    assert session_manager.append_assistant_msg(missing, {"id": "m1"}) is None, (
        "lenient append_assistant_msg should return None"
    )
    assert session_manager.append_user_msg(missing, {"id": "m2", "role": "user"}) is None, (
        "lenient append_user_msg should return None"
    )

    for fn, msg in (
        (session_manager.append_assistant_msg, {"id": "m3"}),
        (session_manager.append_user_msg, {"id": "m4", "role": "user"}),
        # client_id fast path of append_user_msg has its own miss branches.
        (session_manager.append_user_msg, {"id": "m5", "role": "user", "client_id": "c1"}),
    ):
        with pytest.raises(KeyError) as exc:
            fn(missing, msg, strict=True)
        assert "root resolve failed" in str(exc.value), (
            f"strict KeyError should name the rid-miss branch, got: {exc.value}"
        )

    # node-miss branch: root resolvable but tree gone from disk + cache.
    sess = session_manager.create(
        name="strict-node-miss", model="sonnet", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    rid = session_manager._root_id_for(sid)
    assert rid is not None
    (Path(session_store._sessions_dir()) / f"{rid}.json").unlink()
    session_manager.reload_root_from_disk(rid)
    with pytest.raises(KeyError):
        session_manager.append_assistant_msg(sid, {"id": "m6"}, strict=True)


class _CaptureWS:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def __call__(self, frame: dict) -> None:
        self.frames.append(frame)

    def types(self) -> list[str]:
        return [f.get("type") for f in self.frames]


def _bookkeeping_clean(tm, sid: str) -> list[str]:
    leaks = []
    if tm.active_run_ids.get(sid):
        leaks.append(f"active_run_ids={tm.active_run_ids.get(sid)}")
    if tm._run_state.get(sid):
        leaks.append(f"_run_state={tm._run_state.get(sid)}")
    if sid in tm.cancel_events:
        leaks.append("cancel_events")
    if sid in tm.current_turn_workers:
        leaks.append("current_turn_workers")
    if sid in tm.current_assistant_msgs:
        leaks.append("current_assistant_msgs")
    return leaks


def test_missing_root_aborts_before_registration() -> None:
    """run_turn on an unresolvable persist_to raises KeyError with ZERO
    bookkeeping — the prompt-processor barrier stays clean and
    handle_prompt's catch terminates the lifecycle as `failed`."""
    tm = _coordinator().turn_manager
    missing = "11111111-dead-beef-0000-000000000000"
    ws = _CaptureWS()

    async def _drive() -> None:
        await tm.run_turn(
            session={},
            prompt="hello",
            cli_prompt="hello",
            app_session_id=missing,
            model="sonnet",
            cwd="/tmp",
            ws_callback=ws,
            images=None,
            trace_step_name="native",
            session_id_field="agent_session_id",
            mode="native",
        )

    with pytest.raises(KeyError):
        asyncio.run(_drive())

    leaks = _bookkeeping_clean(tm, missing)
    assert not leaks, f"bookkeeping leaked after pre-registration abort: {leaks}"
    assert "user_message_persisted" not in ws.types(), (
        "false persisted-ack emitted for a failed persist"
    )
    assert "turn_start" not in ws.types(), (
        "turn_start emitted for a turn that never registered"
    )


def test_mid_turn_vanish_cleans_bookkeeping() -> None:
    """Root vanishes AFTER the user-msg persist (synchronous listener on
    `user_msg_appended` deletes + evicts) → the strict assistant append
    raises inside the widened try → turn-failure UX runs and the finally
    restores every piece of bookkeeping. Pre-fix code registered the
    phantom target and streamed into it."""
    tm = _coordinator().turn_manager

    sess = session_manager.create(
        name="midturn-vanish", model="sonnet", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    rid = session_manager._root_id_for(sid)
    assert rid is not None
    ws = _CaptureWS()

    def _vanish(fire_sid: str, change: dict) -> None:
        if fire_sid != sid or change.get("kind") != "user_msg_appended":
            return
        try:
            (Path(session_store._sessions_dir()) / f"{rid}.json").unlink()
        except OSError:
            pass
        session_manager.reload_root_from_disk(rid)

    session_manager._listeners.append(_vanish)
    try:
        async def _drive() -> None:
            await tm.run_turn(
                session=session_manager.get_ref(sid),
                prompt="hello",
                cli_prompt="hello",
                app_session_id=sid,
                model="sonnet",
                cwd="/tmp",
                ws_callback=ws,
                images=None,
                trace_step_name="native",
                session_id_field="agent_session_id",
                mode="native",
            )
        asyncio.run(_drive())
    except KeyError:
        # Also acceptable: abort surfaced before the widened try could
        # swallow it (user-msg stage) — bookkeeping assertions below
        # still lock the contract.
        pass
    finally:
        session_manager._listeners.remove(_vanish)

    leaks = _bookkeeping_clean(tm, sid)
    assert not leaks, f"bookkeeping leaked after mid-turn abort: {leaks}"
    runs = tm._run_state.get(sid) or []
    assert not any(r.get("target_message_id") for r in runs), (
        f"phantom target left registered: {runs}"
    )
    # Persist succeeded (frame emitted) so the failure MUST have been
    # reported; if persist never happened neither frame is required.
    assert "error" in ws.types() or "user_message_persisted" in ws.types(), (
        f"turn failed silently: frames={ws.types()}"
    )


def test_pre_begin_claim_cleared_on_abort() -> None:
    """A pre-begin claim must not survive a pre-registration abort."""
    tm = _coordinator().turn_manager
    missing = "22222222-dead-beef-0000-000000000000"
    claim = tm.claim_pre_begin_prompt(missing, "queue-missing", "lifecycle-missing")
    assert claim is not None
    claim.cancel_event.set()
    _coordinator().user_prompt_manager.set_in_flight_lifecycle_msg_id(
        missing,
        "lifecycle-missing",
    )
    ws = _CaptureWS()

    async def _drive() -> None:
        await tm.run_turn(
            session={},
            prompt="hello",
            cli_prompt="hello",
            app_session_id=missing,
            model="sonnet",
            cwd="/tmp",
            ws_callback=ws,
            images=None,
            trace_step_name="native",
            session_id_field="agent_session_id",
            mode="native",
            queue_item_id="queue-missing",
        )

    with pytest.raises(KeyError):
        asyncio.run(_drive())

    _coordinator().user_prompt_manager.clear_in_flight_lifecycle_msg_id(missing)
    assert not tm.has_pre_begin_prompt(missing), (
        "stale pre-begin claim survived the pre-registration abort"
    )


def test_root_writer_guard() -> None:
    """`_migrate_and_persist`'s bulk-walk write routes through the
    registered root-writer guard: a RESIDENT root's stale disk snapshot
    must never be written back (it would clobber live in-memory
    mutations — the turn-loss clobber class), while a NON-resident
    root's backfill must still persist under the root lock."""
    import json

    sess = session_manager.create(
        name="guard-target", model="sonnet", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    rid = session_manager._root_id_for(sid)
    assert rid is not None
    path = Path(session_store._sessions_dir()) / f"{rid}.json"

    assert session_store._root_writer_guard is not None, (
        "session_manager did not register the root writer guard"
    )
    assert rid in session_manager._roots, "freshly created root is not resident — fixture broken"

    # Resident root: the walker's stale snapshot write must be SKIPPED.
    stale = json.loads(path.read_text(encoding="utf-8"))
    stale.pop("_schema_version", None)  # forces the migration dirty flag
    stale["name"] = "stale-clobber"
    session_store._migrate_and_persist(stale)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk.get("name") != "stale-clobber", (
        "resident root was overwritten by a bulk-walk snapshot"
    )

    # Non-resident root: the backfill write must go through.
    session_manager.reload_root_from_disk(rid)
    assert rid not in session_manager._roots, "eviction fixture broken — root still resident"
    stale2 = json.loads(path.read_text(encoding="utf-8"))
    stale2.pop("_schema_version", None)
    stale2["name"] = "backfill-write"
    session_store._migrate_and_persist(stale2)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk.get("name") == "backfill-write", "non-resident backfill write was skipped"
    assert on_disk.get("_schema_version") is not None, (
        "backfill write did not persist the migrated fields"
    )


def test_reload_root_from_disk_releases_persist_coordinator_accepted_root() -> None:
    """Regression: a root vanishing right after a persist was enqueued
    (e.g. redigest rollback, or the `_vanish` listener above) must not
    leak into the persist coordinator's `accepted_roots`.

    `_is_quiescent_unlocked` treats ANY entry in `accepted_roots` as
    "not quiescent" for the WHOLE coordinator, regardless of which root
    it names. Before the fix, `reload_root_from_disk` popped `pending`
    and cancelled the deadline for the reloaded root but never released
    it from `accepted_roots` — leaving one phantom entry that poisons
    every future `shutdown_persistence()` forever, raising "session
    persistence drain is incomplete" even when nothing is actually
    pending."""
    persist = session_manager._persist_coordinator

    sess = session_manager.create(
        name="reload-quiescence", model="sonnet", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    rid = session_manager._root_id_for(sid)
    assert rid is not None

    # Mirror the `_vanish` listener: a persist gets enqueued for this root,
    # then the root is reloaded from disk before the debounce fires.
    with persist.lock:
        persist.enqueue_unlocked(
            rid, session_manager._roots[rid], now=time.monotonic(), debounce=999,
        )
    assert rid in persist.accepted_roots, "fixture broken: enqueue did not accept the root"

    session_manager.reload_root_from_disk(rid)

    with persist.lock:
        assert rid not in persist.accepted_roots, (
            "reload_root_from_disk leaked the reloaded root into "
            "accepted_roots — this permanently blocks quiescence"
        )
        assert rid not in persist.pending
        assert rid not in persist.deadlines


def main() -> int:
    test_append_contracts()
    print(f"{PASS} append strict/lenient contracts")
    test_missing_root_aborts_before_registration()
    print(f"{PASS} missing root aborts pre-registration, zero bookkeeping, no false ack")
    test_mid_turn_vanish_cleans_bookkeeping()
    print(f"{PASS} mid-turn vanish aborts loudly and restores bookkeeping")
    test_pre_begin_claim_cleared_on_abort()
    print(f"{PASS} pre-registration abort clears the exact pre-begin claim")
    test_root_writer_guard()
    print(f"{PASS} root writer guard (resident skip / non-resident persist)")
    test_reload_root_from_disk_releases_persist_coordinator_accepted_root()
    print(f"{PASS} reload_root_from_disk releases the persist coordinator's accepted_roots entry")
    return 0


if __name__ == "__main__":
    sys.exit(main())
