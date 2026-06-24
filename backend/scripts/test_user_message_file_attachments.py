from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-user-files-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchestrator import Coordinator  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_user_message_persists_file_metadata_without_data() -> bool:
    coordinator = Coordinator()
    session = session_manager.create(
        name="files",
        model="sonnet",
        cwd="/tmp/project",
        orchestration_mode="native",
        source="test",
    )
    sid = session["id"]
    msg = coordinator._init_turn_messages(
        session=session,
        app_session_id=sid,
        prompt="read this",
        images=None,
        files=[{
            "name": "notes.txt",
            "media_type": "text/plain",
            "size": 11,
            "data": "aGVsbG8gd29ybGQ=",
        }],
        client_id="pending-1",
        lifecycle_msg_id="life-1",
    )
    fresh = session_manager.get(sid)
    stored = fresh["messages"][0]
    return (
        msg["files"] == [{
            "name": "notes.txt",
            "media_type": "text/plain",
            "size": 11,
        }]
        and stored["files"] == msg["files"]
        and "data" not in stored["files"][0]
        and stored["client_id"] == "pending-1"
    )


def test_malformed_file_attachment_fails_closed() -> bool:
    coordinator = Coordinator()
    session = session_manager.create(
        name="bad",
        model="sonnet",
        cwd="/tmp/project",
        orchestration_mode="native",
        source="test",
    )
    try:
        coordinator._init_turn_messages(
            session=session,
            app_session_id=session["id"],
            prompt="bad",
            images=None,
            files=[{"name": "bad.txt", "media_type": "text/plain"}],
        )
    except ValueError:
        return True
    return False


def main() -> int:
    tests = [
        test_user_message_persists_file_metadata_without_data,
        test_malformed_file_attachment_fails_closed,
    ]
    failures = []
    for test in tests:
        ok = test()
        print(f"{PASS if ok else FAIL} {test.__name__}")
        if not ok:
            failures.append(test.__name__)
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if failures:
        print(f"{FAIL} failures: {', '.join(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
