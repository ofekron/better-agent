from __future__ import annotations

import hmac
import os
import threading
from pathlib import Path

from json_store import read_json, write_json
from paths import ba_home


_LOCK = threading.RLock()
_TOKEN_ENV = "BETTER_AGENT_TESTAPE_QA_TOKEN"
_RUNTIME_ENV = "BETTER_AGENT_TESTAPE_QA_RUNTIME_ID"
_expected_token: str | None = None
_runtime_id: str | None = None


def _registry_path() -> Path:
    return ba_home() / "testape-qa-runtime.json"


def authorize(token: str) -> str:
    global _expected_token, _runtime_id
    with _LOCK:
        if _expected_token is None:
            _expected_token = os.environ.pop(_TOKEN_ENV, "")
            _runtime_id = os.environ.pop(_RUNTIME_ENV, "")
        expected = _expected_token
        runtime_id = _runtime_id
    if not expected or not runtime_id or not hmac.compare_digest(token, expected):
        raise PermissionError("invalid TestApe QA capability")
    return runtime_id


def claim(runtime_id: str, session_id: str) -> None:
    with _LOCK:
        path = _registry_path()
        state = read_json(path, default={})
        if path.exists() and not state:
            raise PermissionError("TestApe QA ownership registry is invalid")
        if state and state.get("runtime_id") != runtime_id:
            raise PermissionError("TestApe QA runtime ownership mismatch")
        sessions = set(state.get("session_ids") or [])
        sessions.add(session_id)
        write_json(path, {
            "runtime_id": runtime_id,
            "session_ids": sorted(sessions),
        })


def assert_owned(runtime_id: str, *session_ids: str) -> None:
    with _LOCK:
        state = read_json(_registry_path(), default={})
    owned = set(state.get("session_ids") or [])
    if state.get("runtime_id") != runtime_id or any(
        not session_id or session_id not in owned
        for session_id in session_ids
    ):
        raise PermissionError("session is not owned by this TestApe QA runtime")
