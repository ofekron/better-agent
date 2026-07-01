from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-msgdelta-compact-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from messages_delta_compaction import compact_message_delta_payload  # noqa: E402
from orchestrator import Coordinator  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_compacts_render_events_without_mutating_source() -> bool:
    msg = {
        "id": "msg-1",
        "role": "assistant",
        "content": "done",
        "events": [{"type": "agent_message", "data": {"uuid": "e1"}}],
        "workers": [
            {
                "delegation_id": "d1",
                "worker_session_id": "w1",
                "events": [{"type": "agent_message", "data": {"uuid": "we1"}}],
                "success": True,
            },
            {"delegation_id": "d2", "worker_session_id": "w2"},
        ],
    }

    payload = compact_message_delta_payload(msg)

    ok = (
        "events" not in payload
        and payload.get("event_payload_omitted") is True
        and payload["content"] == "done"
        and "events" not in payload["workers"][0]
        and payload["workers"][0]["success"] is True
        and payload["workers"][1]["worker_session_id"] == "w2"
        and msg["events"][0]["data"]["uuid"] == "e1"
        and msg["workers"][0]["events"][0]["data"]["uuid"] == "we1"
    )
    print(
        f"{PASS if ok else FAIL} compact messages_delta omits render events "
        "while preserving final fields",
    )
    return ok


def test_orchestrator_uses_shared_compaction_helper() -> bool:
    coordinator = Coordinator.__new__(Coordinator)
    msg = {"id": "msg-1", "events": [1]}
    payload = coordinator._messages_delta_payload(
        msg,
        omit_render_events=True,
    )
    ok = payload == compact_message_delta_payload(msg)
    print(f"{PASS if ok else FAIL} orchestrator uses shared compaction helper")
    return ok


def test_passthrough_when_not_compacting() -> bool:
    coordinator = Coordinator.__new__(Coordinator)
    msg = {"id": "msg-1", "events": [1]}
    payload = coordinator._messages_delta_payload(
        msg,
        omit_render_events=False,
    )
    ok = payload is msg
    print(f"{PASS if ok else FAIL} non-compacted messages_delta is passthrough")
    return ok


def main() -> int:
    try:
        tests = [
            test_compacts_render_events_without_mutating_source,
            test_orchestrator_uses_shared_compaction_helper,
            test_passthrough_when_not_compacting,
        ]
        return 0 if all(test() for test in tests) else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
