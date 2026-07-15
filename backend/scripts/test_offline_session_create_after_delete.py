from __future__ import annotations

import os
import shutil
import sys
import uuid
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-offline-session-create-after-delete-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_BACKEND = str(Path(__file__).resolve().parent.parent)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

from auth_test_helpers import authenticate_client  # noqa: E402
import main  # noqa: E402
import runtime_tokens  # noqa: E402
from bff_runtime_contract import BFF_SERVICE_TOKEN_HEADER  # noqa: E402


def main_runner() -> int:
    """A stale offline-queue `create_session` entry (queued before the user
    deleted the session, replayed after a reconnect) must NOT resurrect the
    deleted session under the same id."""
    client_session_id = str(uuid.uuid4())
    body = {
        "client_session_id": client_session_id,
        "name": "offline",
        "cwd": "/tmp",
        "orchestration_mode": "native",
    }
    with TestClient(main.app, client=("127.0.0.1", 54321)) as client:
        authenticate_client(client)
        headers = {BFF_SERVICE_TOKEN_HEADER: runtime_tokens.ensure_bff_service_token()}
        created = client.post("/api/bff-runtime/sessions", json=body, headers=headers)
        deleted = client.delete(f"/api/sessions/{client_session_id}")
        replayed = client.post("/api/bff-runtime/sessions", json=body, headers=headers)
        listed = client.get("/api/sessions")

    sessions = listed.json()["sessions"]
    ok = (
        created.status_code == 200
        and deleted.status_code == 200
        and replayed.status_code == 409
        and client_session_id not in [s["id"] for s in sessions]
    )
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if not ok:
        print(created.status_code, created.text)
        print(deleted.status_code, deleted.text)
        print(replayed.status_code, replayed.text)
        print(sessions)
        return 1
    print("PASS deleted session id is not resurrected by a replayed offline create")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_runner())
