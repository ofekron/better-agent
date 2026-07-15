"""Regression for the `message_batch` KeyError storm reported on every
failed turn (not just rewind/retry): `run_turn`'s `except Exception`
handler calls `_finalize_turn_messages`, which removes `assistant_msg`
from the persisted session tree (error path always discards the failed
assistant message). The handler then immediately calls
`await ws_callback({"type": "error", ...})`, which re-enters
`save_ws_callback`. Pre-fix, `assistant_msg_holder[0]` still held the
now-orphaned message dict, so `save_ws_callback` tried to apply the
"error" frame to it via `session_manager.message_batch(sid, msg_id)` —
raising `KeyError` for the just-deleted `msg_id` on every single turn
failure (logged as "provider stream journal publish failed").

Run with:
    cd backend && .venv/bin/python scripts/test_turn_error_path_no_stale_msg_apply.py
"""
from __future__ import annotations

import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402
_test_home.isolate("bc-test-turn-error-path-no-stale-msg-apply-")

from session_manager import manager as session_manager  # noqa: E402
import runtime_ownership  # noqa: E402

runtime_ownership.register_current_process_writer()

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_COORD = None


def _coordinator():
    global _COORD
    if _COORD is None:
        import orchestrator
        _COORD = orchestrator.get_active_coordinator() or orchestrator.Coordinator()
    return _COORD


class _CaptureWS:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def __call__(self, frame: dict) -> None:
        self.frames.append(frame)

    def types(self) -> list[str]:
        return [f.get("type") for f in self.frames]


def test_turn_error_after_assistant_append_does_not_key_error() -> bool:
    tm = _coordinator().turn_manager
    sess = session_manager.create(
        name="turn-error-path", model="sonnet", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    ws = _CaptureWS()

    key_errors: list[BaseException] = []
    original_apply = tm._apply_provider_stream_event_sync

    def _spy_apply(*args, **kwargs):
        try:
            return original_apply(*args, **kwargs)
        except KeyError as e:
            key_errors.append(e)
            raise

    tm._apply_provider_stream_event_sync = _spy_apply

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated provider crash after assistant append")

    original_drive = tm._drive_cli_run
    tm._drive_cli_run = _boom
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
    finally:
        tm._drive_cli_run = original_drive
        tm._apply_provider_stream_event_sync = original_apply

    ok = True
    if key_errors:
        print(f"{FAIL} message_batch raised KeyError for the removed assistant msg: {key_errors}")
        ok = False

    current = session_manager.get(sid) or {}
    messages = current.get("messages") or []
    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
    if assistant_msgs:
        print(f"{FAIL} failed assistant message should have been removed, found: {assistant_msgs}")
        ok = False
    if not current.get("unseen_error"):
        print(f"{FAIL} expected session-level unseen_error to be set after turn failure, got: {current.get('unseen_error')!r}")
        ok = False

    if ok:
        print(f"{PASS} turn error after assistant append does not KeyError on the deleted msg")
    return ok


def main() -> int:
    return 0 if test_turn_error_after_assistant_append_does_not_key_error() else 1


if __name__ == "__main__":
    sys.exit(main())
