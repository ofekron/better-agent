"""Regression: a prompt aborted (cancel/shutdown) BEFORE it ever reached
the CLI must terminate as `failed`, NOT `done(cancelled)`.

Bug: session 863899c9 — a prompt+image was persisted, the backend was
cancelled before the runner spawned, and the cancel terminal emitted
`user_message_done(cancelled=True)`. That left the prompt looking
delivered/completed (silent empty bubble) while the agent never saw it.

The fix: `UserPromptManager.emit_user_msg_cancel_terminal` checks whether
the prompt reached `sent` (runner spawned). If it never did, the terminal
is `failed(reason="aborted_before_send")`.

Pre-fix (no `was_sent` gate) this test FAILS: the never-sent case emits
`user_message_done`. Post-fix it emits `user_message_failed`.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc_test_abort_send_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from event_bus import BusEvent, bus  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from user_msg_lifecycle import new_lifecycle_msg_id  # noqa: E402
from user_prompt_manager import UserPromptManager  # noqa: E402


class _StubCoordinator:
    pass


def _capture(name: str):
    events: list[BusEvent] = []

    async def _handler(ev: BusEvent) -> None:
        events.append(ev)

    bus.subscribe("user_message_*", _handler, name=name)
    return events


async def _run() -> None:
    sid = "test-session-abort"
    # Make bus routing resolve a root without a real persisted session.
    session_manager._root_id_for = lambda s: sid  # type: ignore[assignment]

    upm = UserPromptManager(_StubCoordinator())

    # --- Case A: aborted BEFORE send → must be `failed`. ---
    events_a = _capture("cap_a")
    try:
        lid_a = new_lifecycle_msg_id()
        await upm.emit_user_msg_cancel_terminal(sid, lid_a, "native")
    finally:
        bus.unsubscribe("cap_a")

    types_a = [e.type for e in events_a]
    assert "user_message_failed" in types_a, (
        f"never-sent cancel must emit user_message_failed, got {types_a}"
    )
    assert "user_message_done" not in types_a, (
        f"never-sent cancel must NOT emit user_message_done, got {types_a}"
    )
    failed = next(e for e in events_a if e.type == "user_message_failed")
    assert failed.payload.get("reason") == "aborted_before_send", failed.payload

    # --- Case B: sent THEN cancelled → must stay `done(cancelled)`. ---
    events_b = _capture("cap_b")
    try:
        lid_b = new_lifecycle_msg_id()
        upm.mark_sent(lid_b)
        await upm.emit_user_msg_cancel_terminal(sid, lid_b, "native")
    finally:
        bus.unsubscribe("cap_b")

    types_b = [e.type for e in events_b]
    assert "user_message_done" in types_b, (
        f"sent-then-cancel must emit user_message_done, got {types_b}"
    )
    assert "user_message_failed" not in types_b, (
        f"sent-then-cancel must NOT emit user_message_failed, got {types_b}"
    )
    done = next(e for e in events_b if e.type == "user_message_done")
    assert done.payload.get("cancelled") is True, done.payload

    # --- terminal clears the sent marker (no leak across prompts). ---
    assert not upm.was_sent(lid_b), "sent marker must clear on terminal"

    # --- _clear_sent runs even if the emit body raises (finally). ---
    import orchs
    lid_c = new_lifecycle_msg_id()
    upm.mark_sent(lid_c)
    _orig_get = orchs.get_strategy
    orchs.get_strategy = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x"))
    try:
        await upm.emit_user_msg_done(sid, lid_c, "native")
    finally:
        orchs.get_strategy = _orig_get
    assert not upm.was_sent(lid_c), "sent marker must clear even when emit body raises"

    print(
        "OK: aborted-before-send → failed; sent-then-cancel → done(cancelled); "
        "marker clears on terminal + on emit failure"
    )


if __name__ == "__main__":
    asyncio.run(_run())
