"""Regression: rewind_and_retry must be atomic server-side — no WS needed.

The old contract rewound server-side (truncating the failed user+assistant
pair) and returned `retry_prompt` for the FRONTEND to resend over WS. With
no WS connected, the client dropped the prompt: messages ended empty, the
prompt was lost, and the retry button (rendered only on a failed assistant
bubble) was gone.

This locks the fix: with NO WS subscriber attached at all, calling
`rewind_and_retry` must durably re-enqueue the recovered prompt through the
normal queued-prompt path (persisted to disk, recoverable by startup
re-enqueue) and submit it — the session must never end with empty messages
AND no durable trace of the prompt.

Run:
    cd backend && .venv/bin/python scripts/test_retry_reenqueues_without_ws.py
"""

from __future__ import annotations

import os
import shutil
import sys
import asyncio

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-retry-reenqueue-no-ws-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import main  # noqa: E402
import session_manager as _sm_mod  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

_sm_mod.PERSIST_DEBOUNCE_S = 0.0

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


class _FakeProvider:
    supports_rewind = True
    supports_semantic_alter = False
    rewind_requires_agent_identity = False
    defunct = False

    async def rewind(self, session_id: str, anchor: str) -> None:
        return None


def _seed_failed_pair() -> tuple[str, str]:
    root = session_manager.create(name="retry-no-ws", cwd="/tmp")
    sid = root["id"]
    prompt = "please fix the flaky tailer test"
    session_manager.append_user_msg(sid, {
        "id": "u1",
        "role": "user",
        "content": prompt,
        "events": [],
        "timestamp": "2026-07-08T10:00:00",
        "isStreaming": False,
        "agent_message_uuid": "uuid-anchor-1",
    })
    session_manager.append_assistant_msg(sid, {
        "id": "a1",
        "role": "assistant",
        "content": "API Error: The operation timed out.",
        "events": [],
        "timestamp": "2026-07-08T10:00:05",
        "isStreaming": False,
        "stopped_at": "2026-07-08T10:00:05",
    })
    return sid, prompt


def test_retry_reenqueues_durably_without_ws() -> bool:
    sid, prompt = _seed_failed_pair()

    submitted: list[dict] = []

    async def _record_submit(_sid: str, params: dict) -> str:
        submitted.append(params)
        return params.get("_queued_id") or "item"

    original_provider = main.coordinator.provider_for_session
    original_submit = main.coordinator.submit_prompt_async
    main.coordinator.provider_for_session = lambda _sid: _FakeProvider()
    main.coordinator.submit_prompt_async = _record_submit
    try:
        # NO WS subscriber is attached anywhere in this test — the endpoint
        # alone must guarantee the prompt survives.
        body = asyncio.run(main.rewind_and_retry(sid, {
            "assistant_message_id": "a1",
            "client_id": "pending-retry-1",
        }))
    finally:
        main.coordinator.provider_for_session = original_provider
        main.coordinator.submit_prompt_async = original_submit

    # Reload from DISK — durable state, not the in-memory cache.
    session_manager._roots.clear()
    session_manager._node_root_id.clear()
    session_manager._root_locks.clear()
    session_manager._batches.clear()
    current = session_manager.get(sid) or {}
    messages = current.get("messages") or []
    queued = current.get("queued_prompts") or []

    prompt_survives = (
        any(qp.get("content") == prompt for qp in queued)
        or any(
            m.get("role") == "user" and m.get("content") == prompt
            for m in messages
        )
    )
    ok = (
        body.get("ok") is True
        and body.get("enqueued") is True
        and body.get("client_id") == "pending-retry-1"
        and messages == []  # failed pair rewound
        and prompt_survives  # ...but the prompt is durably re-enqueued
        and len(queued) == 1
        and queued[0].get("client_id") == "pending-retry-1"
        and len(submitted) == 1
        and submitted[0].get("prompt") == prompt
        and submitted[0].get("client_id") == "pending-retry-1"
    )
    print(f"{PASS if ok else FAIL} retry re-enqueues prompt durably with no WS subscriber")
    if not ok:
        print({
            "body": body,
            "messages": messages,
            "queued": queued,
            "submitted": submitted,
        })
    return ok


def main_run() -> int:
    try:
        return 0 if test_retry_reenqueues_durably_without_ws() else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main_run())
