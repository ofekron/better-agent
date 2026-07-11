"""POST /api/sessions accepts an optional `folder_id` and files the new
session into that folder at creation time. `null`/omitted means Unfiled.
A stale (non-existent) folder_id is best-effort: the session is still
created, just left Unfiled — so an offline-queued create replayed after
its folder was deleted never fails the whole creation.

Run with:
    cd backend && .venv/bin/python scripts/test_create_session_with_folder.py
"""

from __future__ import annotations

import os
import shutil
import sys
import uuid
from pathlib import Path

_BACKEND = str(Path(__file__).resolve().parent.parent)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402
_TMP_HOME = _test_home.isolate("bc-test-create-session-folder-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from auth_test_helpers import authenticate_client  # noqa: E402
import main  # noqa: E402
import runtime_tokens  # noqa: E402
from bff_runtime_contract import BFF_SERVICE_TOKEN_HEADER  # noqa: E402

CWD = "/tmp/project"
PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _create(client: TestClient, body: dict):
    return client.post(
        "/api/bff-runtime/sessions",
        json=body,
        headers={BFF_SERVICE_TOKEN_HEADER: runtime_tokens.ensure_bff_service_token()},
    )


def _folder_id(client: TestClient, session_id: str) -> str | None:
    """The sidebar resolves a session's folder from the organization
    snapshot's `assignments` (the authoritative source the frontend uses),
    not from the session-listing summary. Read it from there."""
    org = client.get(f"/api/session-organization?project_id={CWD}").json()
    assignment = org.get("assignments", {}).get(session_id, {})
    fid = assignment.get("folder_id")
    return fid if isinstance(fid, str) else None


def main_runner() -> int:
    results: list[tuple[str, bool]] = []

    with TestClient(main.app, client=("127.0.0.1", 54001)) as client:
        authenticate_client(client)

        folder = client.post(
            "/api/session-folders",
            json={"project_id": CWD, "name": "Release"},
        ).json()["folder"]

        # 1. folder_id at creation → session lands in the folder.
        sid_in = str(uuid.uuid4())
        r = _create(client, {
            "client_session_id": sid_in,
            "name": "filed",
            "cwd": CWD,
            "orchestration_mode": "native",
            "folder_id": folder["id"],
        })
        ok = r.status_code == 200 and r.json()["id"] == sid_in
        results.append(("create with folder_id files the session", ok and _folder_id(client, sid_in) == folder["id"]))

        # 2. no folder_id → Unfiled.
        sid_out = str(uuid.uuid4())
        r = _create(client, {
            "client_session_id": sid_out,
            "name": "unfiled",
            "cwd": CWD,
            "orchestration_mode": "native",
        })
        ok = r.status_code == 200
        results.append(("create without folder_id leaves session Unfiled", ok and _folder_id(client, sid_out) is None))

        # 3. explicit null folder_id → Unfiled too.
        sid_null = str(uuid.uuid4())
        _create(client, {
            "client_session_id": sid_null,
            "name": "explicit-null",
            "cwd": CWD,
            "orchestration_mode": "native",
            "folder_id": None,
        })
        results.append(("explicit folder_id=null leaves session Unfiled", _folder_id(client, sid_null) is None))

        # 4. stale folder_id → session still created, left Unfiled (best-effort).
        sid_stale = str(uuid.uuid4())
        r = _create(client, {
            "client_session_id": sid_stale,
            "name": "stale-folder",
            "cwd": CWD,
            "orchestration_mode": "native",
            "folder_id": "folder-does-not-exist",
        })
        ok = r.status_code == 200 and r.json()["id"] == sid_stale
        results.append(("stale folder_id still creates the session (Unfiled)", ok and _folder_id(client, sid_stale) is None))

    all_ok = True
    for label, ok in results:
        print(f"{PASS if ok else FAIL} {label}")
        all_ok = all_ok and ok
    try:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    except Exception:
        pass
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main_runner())
