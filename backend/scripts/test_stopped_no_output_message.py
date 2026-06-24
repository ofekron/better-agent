from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-stopped-empty-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchestrator import Coordinator  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _seed_turn() -> tuple[str, dict, dict, dict]:
    session = session_manager.create(
        name="t",
        model="gpt-5.5",
        cwd="/tmp",
        orchestration_mode="native",
    )
    sid = session["id"]
    user_msg = {
        "id": "user-1",
        "role": "user",
        "content": "interrupt me",
    }
    assistant_msg = {
        "id": "assistant-1",
        "role": "assistant",
        "content": "",
        "events": [],
        "isStreaming": True,
    }
    session_manager.append_user_msg(sid, user_msg)
    session_manager.append_assistant_msg(sid, assistant_msg)
    return sid, session_manager.get(sid), user_msg, assistant_msg


def _finalize(
    *,
    interrupted_by_msg_id: str | None,
) -> dict:
    sid, session, user_msg, assistant_msg = _seed_turn()
    Coordinator._finalize_turn_messages(
        object(),
        session=session,
        app_session_id=sid,
        user_msg=user_msg,
        assistant_msg=assistant_msg,
        primary_result={
            "success": False,
            "events": [],
            "error": "cancelled",
            "sdk_output": "",
        },
        workers=[],
        stopped_at="2026-06-15T00:00:00",
        trace_id="trace-1",
        interrupted_by_msg_id=interrupted_by_msg_id,
    )
    sess = session_manager.get(sid) or {}
    return next(
        m for m in sess.get("messages", [])
        if m.get("id") == assistant_msg["id"]
    )


def _finalize_with_provider_usage() -> dict:
    sid, session, user_msg, assistant_msg = _seed_turn()
    Coordinator._finalize_turn_messages(
        object(),
        session=session,
        app_session_id=sid,
        user_msg=user_msg,
        assistant_msg=assistant_msg,
        primary_result={
            "success": True,
            "events": [],
            "sdk_output": "done",
            "token_usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_read_input_tokens": 5,
                "total_tokens": 12,
            },
        },
        workers=[],
        stopped_at=None,
        trace_id="trace-token",
    )
    return session_manager.get(sid) or {}


def _finalize_success_assistant() -> dict:
    sid, session, user_msg, assistant_msg = _seed_turn()
    Coordinator._finalize_turn_messages(
        object(),
        session=session,
        app_session_id=sid,
        user_msg=user_msg,
        assistant_msg=assistant_msg,
        primary_result={
            "success": True,
            "events": [],
            "sdk_output": "done",
        },
        workers=[],
        stopped_at=None,
        trace_id="trace-completed",
    )
    return assistant_msg


def _finalize_success_with_error_content() -> dict:
    sid, session, user_msg, assistant_msg = _seed_turn()
    Coordinator._finalize_turn_messages(
        object(),
        session=session,
        app_session_id=sid,
        user_msg=user_msg,
        assistant_msg=assistant_msg,
        primary_result={
            "success": True,
            "events": [],
            "sdk_output": "API Error: quota exhausted",
        },
        workers=[],
        stopped_at=None,
        trace_id="trace-error-content",
    )
    return assistant_msg


def main() -> int:
    failures: list[str] = []

    stopped = _finalize(interrupted_by_msg_id=None)
    # A stopped turn leaves content empty — the `stopped_at` indication
    # (rendered as the "Stopped at …" badge) is enough; no artificial
    # placeholder content is added.
    if stopped.get("content") == "":
        print(f"{PASS} stopped no-output turn has empty content")
    else:
        print(f"{FAIL} stopped content={stopped.get('content')!r}")
        failures.append("stopped content")

    interrupted = _finalize(interrupted_by_msg_id="next-user-msg")
    # An interrupted turn leaves content empty — the `interrupted_by_msg_id`
    # indication (rendered as the "Interrupted at …" badge) is enough; no
    # artificial placeholder content is added.
    if interrupted.get("content") == "":
        print(f"{PASS} interrupted no-output turn has empty content")
    else:
        print(f"{FAIL} interrupted content={interrupted.get('content')!r}")
        failures.append("interrupted content")

    if interrupted.get("interrupted_by_msg_id") == "next-user-msg":
        print(f"{PASS} interrupted cross-reference persisted")
    else:
        print(f"{FAIL} interrupted_by={interrupted.get('interrupted_by_msg_id')!r}")
        failures.append("interrupted cross-reference")

    token_session = _finalize_with_provider_usage()
    total = token_session.get("token_usage_total") or {}
    last = token_session.get("token_usage_last") or {}
    if (
        total.get("input_tokens") == 10
        and total.get("output_tokens") == 2
        and total.get("cache_read_input_tokens") == 5
        and last.get("input_tokens") == 10
        and last.get("output_tokens") == 2
        and last.get("cache_read_input_tokens") == 5
    ):
        print(f"{PASS} provider-result token usage persisted")
    else:
        print(f"{FAIL} token_usage_total={total!r} token_usage_last={last!r}")
        failures.append("provider-result token usage")

    completed = _finalize_success_assistant()
    if completed.get("completed_at") and completed.get("content") == "done":
        print(f"{PASS} successful assistant finalized with completed_at")
    else:
        print(f"{FAIL} completed assistant={completed!r}")
        failures.append("success completed_at")

    error_content = _finalize_success_with_error_content()
    if error_content.get("error") and not error_content.get("completed_at"):
        print(f"{PASS} error-looking successful result has no completed_at")
    else:
        print(f"{FAIL} error-content assistant={error_content!r}")
        failures.append("error-content completed_at")

    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
