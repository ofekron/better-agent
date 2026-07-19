#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HOME = tempfile.mkdtemp(prefix="ba-user-input-store-schema-")

import paths  # noqa: E402

paths.engage_test_home(HOME)

import user_input_store  # noqa: E402


def _write_stale_store() -> Path:
    path = user_input_store._path()
    path.write_text(
        json.dumps(
            {
                "requests": {
                    "stale-request-id": {
                        "request_id": "stale-request-id",
                        "app_session_id": "stale-session",
                        "kind": "input",
                        "questions": [],
                        "prompt": "",
                        "status": "pending",
                        "response": {},
                        "created_at": 0,
                        "expires_at": None,
                        "resolved_at": None,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def main() -> None:
    path = _write_stale_store()

    # Reads against the stale (pre-schema_version) store must self-heal
    # instead of raising, since this is a rebuildable pending-request queue.
    counts = user_input_store.pending_counts_by_session()
    assert counts == {}, counts

    pending = user_input_store.pending_requests()
    assert pending == [], pending

    # A subsequent real write must succeed and persist the current schema.
    req = user_input_store.create_request(
        app_session_id="fresh-session",
        questions=[{"id": "q1", "question": "ok?"}],
        timeout_seconds=None,
    )
    assert req["app_session_id"] == "fresh-session", req

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == user_input_store._SCHEMA_VERSION, on_disk
    assert req["request_id"] in on_disk["requests"], on_disk

    print("OK: stale user_input_store schema self-heals and subsequent writes persist")


if __name__ == "__main__":
    main()
