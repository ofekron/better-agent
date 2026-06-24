from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-force-overflow-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from starlette.testclient import TestClient  # noqa: E402
import main  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def _post(client: TestClient, body: dict, token: str | None = None):
    headers = {}
    if token is not None:
        headers["X-Internal-Token"] = token
    return client.post(
        "/api/internal/test/force-context-overflow",
        json=body,
        headers=headers,
    )


def main_test() -> int:
    client = TestClient(main.app, client=("127.0.0.1", 50000))
    token = main.coordinator.internal_token
    sid = session_manager.create(cwd=_TMP_HOME, name="force-overflow")["id"]
    body = {
        "app_session_id": sid,
        "confirm": "FORCE_CONTEXT_OVERFLOW_FOR_TESTING",
    }

    wrong = _post(client, body, token="wrong")
    assert wrong.status_code == 403

    missing_confirm = _post(client, {"app_session_id": sid}, token=token)
    assert missing_confirm.status_code == 400

    unknown = _post(
        client,
        {
            "app_session_id": "missing",
            "confirm": "FORCE_CONTEXT_OVERFLOW_FOR_TESTING",
        },
        token=token,
    )
    assert unknown.status_code == 404

    ok = _post(client, body, token=token)
    assert ok.status_code == 200
    assert ok.json() == {"success": True, "armed": True, "submitted": False}
    assert sid in main.coordinator.turn_manager._forced_context_overflow_once
    fresh = session_manager.get(sid) or {}
    assert fresh.get("continuation_chain") in (None, [])

    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main_test())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
