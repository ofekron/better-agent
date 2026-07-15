from __future__ import annotations

import os
import shutil
import sys
import asyncio
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-rewind-waits-for-active-turn-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import main  # noqa: E402
import session_store  # noqa: E402
import session_manager as _sm_mod  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
import runtime_ownership  # noqa: E402

runtime_ownership.register_current_process_writer()
_sm_mod.PERSIST_DEBOUNCE_S = 0.0

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _reset_home() -> None:
    sessions_dir = Path(_TMP_HOME) / "sessions"
    if sessions_dir.exists():
        shutil.rmtree(sessions_dir)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_store._fork_index.clear()
    session_store._index_loaded = False
    session_store._summary_index.clear()
    session_store._summary_index_loaded = False
    session_manager._roots.clear()
    session_manager._node_root_id.clear()
    session_manager._root_locks.clear()
    session_manager._batches.clear()


def test_rewind_blocks_while_turn_active() -> bool:
    """Regression for the race that produced repeated
    `KeyError` in `session_manager.message_batch`: `rewind_files`
    used to truncate `messages` immediately, even while a turn was
    still actively streaming events for the assistant message being
    discarded. `save_ws_callback` kept targeting the now-deleted
    `msg_id` via `message_batch`, raising `KeyError` for every
    subsequent stream event of that turn.

    `rewind_files` must now wait for `turn_manager.wait_for_clear_runs`
    before mutating `messages`, so truncation can never race a live
    turn's stream-event application.
    """
    _reset_home()
    root = session_manager.create(name="rewind-race", cwd="/tmp")
    sid = root["id"]
    user = {
        "id": "u1", "role": "user", "content": "hi", "events": [],
        "timestamp": "2026-06-16T20:31:53", "isStreaming": False,
    }
    assistant = {
        "id": "a1", "role": "assistant", "content": "", "events": [],
        "timestamp": "2026-06-16T20:32:00", "isStreaming": True,
    }
    session_manager.append_user_msg(sid, user)
    session_manager.append_assistant_msg(sid, assistant)

    run_id = "run-1"
    main.coordinator.turn_manager.active_run_ids[sid] = [run_id]
    main.coordinator.turn_manager.run_state_add(
        sid, run_id=run_id, kind="manager", target_message_id="a1",
        foreground_status="running",
    )

    order: list[str] = []

    async def _drive() -> dict:
        rewind_task = asyncio.create_task(
            main.coordinator.rewind_files(sid, "u1", provider_rewind=False)
        )
        # Give rewind_files a chance to run — it must block on the
        # barrier rather than truncating immediately.
        await asyncio.sleep(0.2)
        still_present = any(m.get("id") == "a1" for m in (
            session_manager.get(sid) or {}
        ).get("messages", []))
        order.append("blocked-while-running" if still_present else "raced")

        main.coordinator.turn_manager.run_state_remove(sid, run_id)
        main.coordinator.turn_manager.active_run_ids.pop(sid, None)
        order.append("cleared")

        result = await asyncio.wait_for(rewind_task, timeout=5.0)
        return result

    result = asyncio.run(_drive())

    current = session_manager.get(sid) or {}
    messages = current.get("messages") or []
    ok = (
        order == ["blocked-while-running", "cleared"]
        and [m.get("id") for m in messages] == []
        and result.get("messages") == []
    )
    print(f"{PASS if ok else FAIL} rewind_files waits for active turn before truncating")
    if not ok:
        print({"order": order, "messages": messages, "result": result})
    return ok


def main_run() -> int:
    try:
        return 0 if test_rewind_blocks_while_turn_active() else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main_run())
