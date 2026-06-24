from __future__ import annotations

import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-offline-session-create-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_BACKEND = str(Path(__file__).resolve().parent.parent)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

from auth_test_helpers import authenticate_client  # noqa: E402
import main  # noqa: E402


def main_runner() -> int:
    client_session_id = str(uuid.uuid4())
    body = {
        "client_session_id": client_session_id,
        "name": "offline",
        "cwd": "/tmp",
        "orchestration_mode": "native",
    }
    with TestClient(main.app, client=("127.0.0.1", 54321)) as client:
        authenticate_client(client)
        first = client.post("/api/sessions", json=body)
        second = client.post("/api/sessions", json=body)
        listed = client.get("/api/sessions")

    sessions = listed.json()["sessions"]
    ok = (
        first.status_code == 200
        and second.status_code == 200
        and first.json()["id"] == client_session_id
        and second.json()["id"] == client_session_id
        and [s["id"] for s in sessions].count(client_session_id) == 1
    )
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if not ok:
        print(first.status_code, first.text)
        print(second.status_code, second.text)
        print(sessions)
        return 1
    print("PASS offline session creation is idempotent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_runner())
