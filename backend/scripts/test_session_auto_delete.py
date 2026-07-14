from __future__ import annotations

import os
import shutil
import sys
import tempfile
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-session-auto-delete-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import auth  # noqa: E402
import session_store  # noqa: E402
import user_prefs  # noqa: E402
import runtime_tokens  # noqa: E402
from bff_runtime_contract import BFF_SERVICE_TOKEN_HEADER  # noqa: E402
from paths import ba_home  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _bff_headers() -> dict[str, str]:
    return {BFF_SERVICE_TOKEN_HEADER: runtime_tokens.ensure_bff_service_token()}


def _reset_home() -> None:
    for name in ("sessions", "runs"):
        path = Path(_TMP_HOME) / name
        if path.exists():
            shutil.rmtree(path)
    (Path(_TMP_HOME) / "sessions").mkdir(parents=True, exist_ok=True)
    prefs_path = Path(_TMP_HOME) / "user_prefs.json"
    prefs_path.unlink(missing_ok=True)
    session_store._fork_index.clear()
    session_store._index_loaded = False
    session_manager._roots.clear()
    session_manager._node_root_id.clear()
    session_manager._root_locks.clear()
    session_manager._batches.clear()


def _create(client: TestClient, name: str) -> str:
    r = client.post(
        "/api/bff-runtime/sessions",
        json={"name": name, "cwd": "/tmp"},
        headers=_bff_headers(),
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _set_updated_at(sid: str, when: datetime) -> None:
    root = session_manager.get(sid)
    assert root is not None
    root["updated_at"] = when.isoformat()
    session_store.write_session_full(root, bump_updated_at=False)
    session_manager._roots.pop(sid, None)


def test_default_never_and_persistence(client: TestClient) -> bool:
    _reset_home()
    r = client.get("/api/bff-runtime/preferences", headers=_bff_headers())
    if r.status_code != 200:
        print(f"  prefs get failed: {r.status_code} {r.text}")
        return False
    if r.json().get("session_auto_delete_days", "missing") is not None:
        print(f"  default mismatch: {r.json()}")
        return False
    r = client.patch("/api/bff-runtime/preferences", json={"session_auto_delete_days": 30}, headers=_bff_headers())
    if r.status_code != 200:
        print(f"  prefs patch failed: {r.status_code} {r.text}")
        return False
    if user_prefs.get_session_auto_delete_days() != 30:
        print(f"  persisted value mismatch: {ba_home() / 'user_prefs.json'}")
        return False
    r = client.patch("/api/bff-runtime/preferences", json={"session_auto_delete_days": None}, headers=_bff_headers())
    if r.status_code != 200 or user_prefs.get_session_auto_delete_days() is not None:
        print(f"  clearing to never failed: {r.status_code} {r.text}")
        return False
    return True


def test_invalid_values_rejected(client: TestClient) -> bool:
    _reset_home()
    for value in (0, -1, True, "7"):
        r = client.patch("/api/bff-runtime/preferences", json={"session_auto_delete_days": value}, headers=_bff_headers())
        if r.status_code != 400:
            print(f"  invalid value accepted: {value!r} -> {r.status_code}")
            return False
    return True


def test_prunes_only_expired_non_running_sessions(client: TestClient) -> bool:
    _reset_home()
    old_sid = _create(client, "old")
    fresh_sid = _create(client, "fresh")
    running_sid = _create(client, "running")
    _set_updated_at(old_sid, datetime.now() - timedelta(days=31))
    _set_updated_at(fresh_sid, datetime.now() - timedelta(days=5))
    _set_updated_at(running_sid, datetime.now() - timedelta(days=31))

    original = main.coordinator.turn_manager.is_running_cached
    main.coordinator.turn_manager.is_running_cached = lambda sid: sid == running_sid
    try:
        r = client.patch("/api/bff-runtime/preferences", json={"session_auto_delete_days": 30}, headers=_bff_headers())
        if r.status_code != 200:
            print(f"  prefs patch failed: {r.status_code} {r.text}")
            return False
        asyncio.run(main._auto_delete_expired_sessions())
        r = client.get("/api/sessions")
        if r.status_code != 200:
            print(f"  sessions get failed: {r.status_code} {r.text}")
            return False
        ids = {s["id"] for s in r.json()["sessions"]}
    finally:
        main.coordinator.turn_manager.is_running_cached = original

    if old_sid in ids:
        print(f"  expired session was not pruned: {ids}")
        return False
    if fresh_sid not in ids or running_sid not in ids:
        print(f"  non-expired/running session pruned: {ids}")
        return False
    return True


def main_test() -> int:
    client = TestClient(main.app, client=("127.0.0.1", 50000))
    client.headers.update({"Authorization": f"Bearer {auth.create_token('session-auto-delete-test')}"})
    tests = [
        ("default never + persistence", test_default_never_and_persistence),
        ("invalid values rejected", test_invalid_values_rejected),
        ("prunes only expired non-running sessions", test_prunes_only_expired_non_running_sessions),
    ]
    ok = True
    for name, fn in tests:
        passed = fn(client)
        print(f"{PASS if passed else FAIL} {name}")
        ok = ok and passed
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main_test())
