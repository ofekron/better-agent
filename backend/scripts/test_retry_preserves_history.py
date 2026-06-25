from __future__ import annotations

import os
import shutil
import sys
import tempfile
import asyncio
import base64
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-retry-preserves-history-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import main  # noqa: E402
import session_store  # noqa: E402
import session_manager as _sm_mod  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from paths import ba_home  # noqa: E402

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


def test_retry_preserves_previous_turn() -> bool:
    _reset_home()
    root = session_manager.create(name="retry", cwd="/tmp")
    sid = root["id"]
    raw_image = b"retry-image"
    image_path = ba_home() / "sessions" / "images" / sid / "u1_0.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(raw_image)
    user = {
        "id": "u1",
        "role": "user",
        "content": "codex state missing native rollout path",
        "events": [],
        "timestamp": "2026-06-16T20:31:53",
        "isStreaming": False,
        "images": [{"filename": "u1_0.png", "media_type": "image/png"}],
    }
    assistant = {
        "id": "a1",
        "role": "assistant",
        "content": "failed before completion",
        "events": [],
        "timestamp": "2026-06-16T20:32:00",
        "isStreaming": False,
        "stopped_at": "2026-06-16T20:32:00",
    }
    session_manager.append_user_msg(sid, user)
    session_manager.append_assistant_msg(sid, assistant)

    async def noop_rewind_files(*_args, **_kwargs):
        return None

    submitted: list[dict] = []

    async def _record_submit(_sid: str, params: dict) -> str:
        submitted.append(params)
        return params.get("_queued_id") or "item"

    original = main.coordinator.rewind_files
    original_submit = main.coordinator.submit_prompt_async
    main.coordinator.rewind_files = noop_rewind_files
    main.coordinator.submit_prompt_async = _record_submit
    try:
        body = asyncio.run(
            main.rewind_and_retry(
                sid,
                {"assistant_message_id": "a1"},
            )
        )
    finally:
        main.coordinator.rewind_files = original
        main.coordinator.submit_prompt_async = original_submit

    expected_images = [{
        "data": base64.b64encode(raw_image).decode("ascii"),
        "media_type": "image/png",
    }]
    current = session_manager.get(sid) or {}
    messages = current.get("messages") or []
    queued = current.get("queued_prompts") or []
    ok = (
        body.get("ok") is True
        and body.get("enqueued") is True
        and len(queued) == 1  # prompt durably re-enqueued server-side
        and queued[0].get("content") == user["content"]
        and queued[0].get("images") == expected_images
        and len(submitted) == 1
        and submitted[0].get("prompt") == user["content"]
        and submitted[0].get("images") == expected_images
        and [m.get("id") for m in messages] == ["u1", "a1"]
        and current.get("next_seq") == 2
    )
    print(f"{PASS if ok else FAIL} retry preserves previous user+assistant turn")
    if not ok:
        print({
            "body": body,
            "messages": messages,
            "next_seq": current.get("next_seq"),
            "queued": queued,
            "submitted": submitted,
        })
    return ok


def main_run() -> int:
    try:
        return 0 if test_retry_preserves_previous_turn() else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main_run())
