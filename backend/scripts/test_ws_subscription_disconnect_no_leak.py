"""Regression: a WS socket subscribes to MANY sessions; on disconnect the
backend must unregister EVERY one — not just the last id touched.

The bug: `websocket_chat`'s disconnect cleanup unregistered only the single
`current_app_session_id` slot (overwritten on every subscribe/send). A real
tab subscribes to the focused pane PLUS every `additionalAppSessionIds`
pane, so the non-last subscriptions leaked in `ws_callbacks` /
`_subscriber_index`. Because subscription keys were `id(ws_callback)` —
a CPython memory address that is RECYCLED after the dead connection's
closure is GC'd — a reconnected tab's fresh callback could collide with a
leaked entry, and `_subscribe_to_wire_tailer`'s dedup guard then SKIPPED
the new subscribe. Net effect: the focused session received no live
(tailer-only) events until the user manually switched sessions (which sends
an explicit `unsubscribe` that pops the stale key). Content frames
(`manager_event`/`messages_delta`/`turn_start`) reach a *viewing* tab ONLY
via the tailer subscriber (they are excluded from `_BRIDGE_EVENT_TYPES`),
so a dropped subscription = a dead view.

Fix:
  1. `Coordinator.unregister_all_ws(cb)` drops the callback from
     EVERY session it is registered for (called on disconnect).
  2. Subscription bookkeeping keys on a per-connection token
     (`_cb_token`, stamped as `_bc_conn_token`) instead of `id()`, so a
     recycled address can never collide.

This test drives a REAL `BetterAgentJsonlTailer` (per-root `tail`
subprocess) via the public `register_ws` API and asserts the coordinator's
subscription registries — no delivery-timing dependence.

Pre-fix this fails: `unregister_all_ws` does not exist (AttributeError).

Run:
    cd backend && .venv/bin/python scripts/test_ws_subscription_disconnect_no_leak.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# State-isolation rule: set BETTER_CLAUDE_HOME BEFORE importing backend.
import _test_home
_test_home.isolate("bc-test-wsleak-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runtime_ownership  # noqa: E402
runtime_ownership.register_current_process_writer()

from session_manager import manager as session_manager  # noqa: E402
from orchestrator import Coordinator, _cb_token  # noqa: E402
from paths import ba_home  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _mk_root() -> str:
    """Create a root session + an empty events.jsonl so the per-root
    `tail -f` wire tailer starts cleanly."""
    sess = session_manager.create(
        name="t", model="glm-5.1", cwd="/tmp", orchestration_mode="native",
    )
    sid = sess["id"]
    ev = ba_home() / "sessions" / sid / "events.jsonl"
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.touch()
    return sid


def _make_cb(token: str):
    async def cb(_event_dict):  # noqa: ANN001
        return None
    cb._bc_conn_token = token  # type: ignore[attr-defined]
    return cb


async def _settle() -> None:
    # Let register_ws's create_task(_subscribe_to_wire_tailer) run.
    for _ in range(25):
        await asyncio.sleep(0.02)


def _is_subscribed(coord: Coordinator, sid: str, cb) -> bool:
    return (
        (sid, _cb_token(cb)) in coord._subscriber_index
        or cb in coord.ws_callbacks.get(sid, [])
    )


async def test_disconnect_unregisters_every_subscribed_session() -> bool:
    """One socket subscribes to two sessions; the disconnect cleanup must
    drop BOTH. Documents that the old single-id cleanup leaks the other."""
    coord = Coordinator()
    f_sid, a_sid = _mk_root(), _mk_root()
    cb = _make_cb("conn-1")

    coord.register_ws(f_sid, cb)
    coord.register_ws(a_sid, cb)
    await _settle()

    both_attached = _is_subscribed(coord, f_sid, cb) and _is_subscribed(coord, a_sid, cb)

    # Pre-fix disconnect: only the LAST id (a_sid) was unregistered → f_sid
    # leaks. Assert the leak so the test documents the bug it guards.
    coord.unregister_ws(a_sid, cb)
    leaked_before_fix = _is_subscribed(coord, f_sid, cb)

    # The fix: drop every remaining session this socket holds.
    coord.unregister_all_ws(cb)
    await _settle()
    fully_cleaned = (
        not _is_subscribed(coord, f_sid, cb)
        and not _is_subscribed(coord, a_sid, cb)
    )

    ok = both_attached and leaked_before_fix and fully_cleaned
    print(f"  {PASS if ok else FAIL} disconnect unregisters all sessions "
          f"(attached={both_attached}, leak-without-fix={leaked_before_fix}, "
          f"cleaned={fully_cleaned})")
    return ok


async def test_per_connection_token_isolation() -> bool:
    """Two connections (distinct tokens) subscribe to the SAME session.
    Cleaning up one must not drop the other — the property that, combined
    with full cleanup, makes a reconnect's re-subscribe always take."""
    coord = Coordinator()
    f_sid = _mk_root()
    cb1, cb2 = _make_cb("conn-1"), _make_cb("conn-2")

    coord.register_ws(f_sid, cb1)
    coord.register_ws(f_sid, cb2)
    await _settle()
    both = _is_subscribed(coord, f_sid, cb1) and _is_subscribed(coord, f_sid, cb2)

    coord.unregister_all_ws(cb1)
    await _settle()
    cb1_gone = not _is_subscribed(coord, f_sid, cb1)
    cb2_survives = _is_subscribed(coord, f_sid, cb2)

    coord.unregister_all_ws(cb2)
    await _settle()

    ok = both and cb1_gone and cb2_survives
    print(f"  {PASS if ok else FAIL} per-connection token isolation "
          f"(both={both}, cb1_gone={cb1_gone}, cb2_survives={cb2_survives})")
    return ok


async def _main() -> int:
    results = [
        await test_disconnect_unregisters_every_subscribed_session(),
        await test_per_connection_token_isolation(),
    ]
    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
