"""Security regression: the old test auth bypass env var is inert.

`BETTER_CLAUDE_TEST_AUTH_BYPASS=1` used to allow unauthenticated
loopback `/api/*` access. That is no longer permitted: auth behavior
must be identical whether the env var is set or unset.

Checks:
  1. bypass=1 + loopback REST -> 401
  2. bypass=1 + remote REST   -> 401
  3. bypass=1 + loopback WS   -> close 1008

Run with:
    cd backend && .venv/bin/python scripts/test_auth_bypass_loopback_only.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-authbypass-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from starlette.testclient import TestClient, WebSocketDisconnect  # noqa: E402
import main  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_LOOPBACK = TestClient(main.app, client=("127.0.0.1", 50000))
_REMOTE = TestClient(main.app, client=("203.0.113.7", 50000))  # TEST-NET-3


def _sessions_status(client: TestClient) -> int:
    return client.get("/api/sessions").status_code


def _me_status(client: TestClient) -> int:
    return client.get("/api/auth/me").status_code


def _ws_close_code(client: TestClient) -> int | None:
    try:
        with client.websocket_connect("/ws/chat") as ws:
            ws.receive_json()
    except WebSocketDisconnect as exc:
        return exc.code
    return None


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []

    s_local = _sessions_status(_LOOPBACK)
    results.append((
        "bypass=1 + loopback REST -> 401",
        s_local == 401,
        f"got {s_local}",
    ))

    me_local = _me_status(_LOOPBACK)
    results.append((
        "bypass=1 + loopback /api/auth/me -> 401",
        me_local == 401,
        f"got {me_local}",
    ))

    s_remote = _sessions_status(_REMOTE)
    results.append((
        "bypass=1 + remote REST -> 401",
        s_remote == 401,
        f"got {s_remote}",
    ))

    ws_code = _ws_close_code(_LOOPBACK)
    results.append((
        "bypass=1 + loopback WS -> close 1008",
        ws_code == 1008,
        f"got {ws_code}",
    ))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' — ' + detail}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def main_() -> int:
    try:
        return 0 if _run() else 1
    finally:
        os.environ.pop("BETTER_CLAUDE_TEST_AUTH_BYPASS", None)
        os.environ.pop("BETTER_CLAUDE_API_ONLY", None)
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main_())
