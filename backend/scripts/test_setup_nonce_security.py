#!/usr/bin/env python3
"""Test backend/setup_nonce.py and the remote-origin nonce gate on /api/auth/setup.

Dual-purpose: pytest-collectable (real asserts) and standalone
(``python scripts/test_setup_nonce_security.py``).
"""
from __future__ import annotations

import os
import shutil
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-setup-nonce-")

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


def test_remote_setup_without_nonce_denied() -> None:
    """A remote (non-loopback) client hitting /api/auth/setup without the
    one-time setup nonce is rejected with 403."""
    prior = auth._BOOTSTRAPPED
    auth._BOOTSTRAPPED = False
    try:
        client = TestClient(main.app, client=("203.0.113.7", 50000), base_url="http://localhost:8000")
        res = client.post("/api/auth/setup", json={"username": "u", "password": "p"})
    finally:
        auth._BOOTSTRAPPED = prior
    assert res.status_code == 403, f"expected 403, got {res.status_code}: {res.text[:120]}"


def test_nonce_single_use() -> None:
    """A minted nonce consumes exactly once; replay is rejected."""
    nonce = setup_nonce.mint()
    assert setup_nonce.consume(nonce), "first consume failed"
    assert not setup_nonce.consume(nonce), "replay consume succeeded"


def test_nonce_rejects_wrong_value() -> None:
    """A wrong nonce value is rejected and leaves the real one unconsumed."""
    setup_nonce.mint()
    assert not setup_nonce.consume("wrong"), "wrong nonce accepted"


TESTS = [
    ("remote /api/auth/setup without nonce denied", test_remote_setup_without_nonce_denied),
    ("setup nonce is single-use", test_nonce_single_use),
    ("setup nonce rejects wrong value", test_nonce_rejects_wrong_value),
]


def main_run() -> int:
    failed = 0
    for name, fn in TESTS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print(f"  {FAIL} {name} - {exc}")
            failed += 1
            continue
        print(f"  {PASS} {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
