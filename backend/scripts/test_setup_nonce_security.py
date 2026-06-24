#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-setup-nonce-")
os.environ.pop("BETTER_CLAUDE_TEST_AUTH_BYPASS", None)

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from starlette.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import auth  # noqa: E402
import setup_nonce  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_remote_setup_without_nonce_denied() -> tuple[bool, str]:
    auth._BOOTSTRAPPED = False  # type: ignore[attr-defined]
    client = TestClient(main.app, client=("203.0.113.7", 50000), base_url="http://localhost:8000")
    res = client.post("/api/auth/setup", json={"username": "u", "password": "p"})
    return res.status_code == 403, f"expected 403, got {res.status_code}: {res.text[:120]}"


def test_nonce_single_use() -> tuple[bool, str]:
    nonce = setup_nonce.mint()
    if not setup_nonce.consume(nonce):
        return False, "first consume failed"
    if setup_nonce.consume(nonce):
        return False, "replay consume succeeded"
    return True, ""


def test_nonce_rejects_wrong_value() -> tuple[bool, str]:
    setup_nonce.mint()
    if setup_nonce.consume("wrong"):
        return False, "wrong nonce accepted"
    return True, ""


TESTS = [
    ("remote /api/auth/setup without nonce denied", test_remote_setup_without_nonce_denied),
    ("setup nonce is single-use", test_nonce_single_use),
    ("setup nonce rejects wrong value", test_nonce_rejects_wrong_value),
]


def main_run() -> int:
    failed = 0
    for name, fn in TESTS:
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"exception: {exc}"
        print(f"  {PASS if ok else FAIL} {name}{'' if ok else ' - ' + detail}")
        if not ok:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
