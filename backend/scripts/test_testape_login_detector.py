#!/usr/bin/env python3
"""Tests for the TestApe-backed login detector (backend/testape_login_detector.py).

Logic + loopback-guard cases run anywhere. The live case drives a real connected
TestApe web adapter through the SDK and is skipped when none is available.

Run standalone:  python scripts/test_testape_login_detector.py
"""
from __future__ import annotations

import os
import sys
import tempfile

import _test_home
_test_home.isolate("bc-test-testape-login-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import testape_login_detector as det  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
SKIP = "\x1b[33mSKIP\x1b[0m"

ALLOWED_STATES = {"login", "setup", "authenticated", "unknown"}


# ── classifier logic (no environment needed) ────────────────────────────────

def test_classify_authenticated():
    s = det._classify("a", {"auth": {"ok": True, "status": 200}})
    assert s.state == "authenticated" and s.logged_in is True


def test_classify_login_vs_setup():
    base = {"auth": {"ok": False, "status": 401}, "loginShell": True}
    assert det._classify("a", {**base, "passwordAutoComplete": "current-password"}).state == "login"
    assert det._classify("a", {**base, "passwordAutoComplete": "new-password"}).state == "setup"
    # Missing autoComplete still reads as the returning-user login form.
    assert det._classify("a", {**base, "passwordAutoComplete": None}).state == "login"


def test_classify_dom_priority_over_authenticated_endpoint():
    # A rendered login form wins over a 200 (DOM is the ground truth for a login detector).
    s = det._classify("a", {"auth": {"ok": True, "status": 200}, "loginShell": True,
                            "passwordAutoComplete": "current-password"})
    assert s.state == "login" and s.logged_in is False


def test_classify_unknown_when_logged_out_without_form():
    s = det._classify("a", {"auth": {"ok": False, "status": 401}, "loginShell": False})
    assert s.state == "unknown" and s.logged_in is False


def test_classify_unknown_when_app_unreachable():
    s = det._classify("a", {"auth": {"status": None}, "loginShell": False})
    assert s.state == "unknown" and s.logged_in is False
    assert s.reason and "unreachable" in s.reason


# ── loopback navigation guard (security) ────────────────────────────────────

def test_loopback_guard_accepts_localhost():
    det._assert_loopback("http://localhost:8000")
    det._assert_loopback("http://127.0.0.1:8000/")


def test_loopback_guard_rejects_external():
    for bad in ("http://example.com/", "https://1.2.3.4/", "javascript:alert(1)"):
        with pytest.raises(ValueError):
            det._assert_loopback(bad)


# ── live SDK round-trip (skipped without a connected web adapter) ───────────

def _live_adapter():
    try:
        adapters = det.list_web_adapters(det.FS_DEFAULT)
    except Exception:
        return None
    return adapters[0][0] if adapters else None


def test_detect_live_adapter():
    adapter_id = _live_adapter()
    if not adapter_id:
        pytest.skip("no connected TestApe web adapter")

    state = det.detect_login_state(adapter_id)
    assert state.adapter_id == adapter_id
    assert state.state in ALLOWED_STATES
    # logged_in must be consistent with the detected state.
    if state.state == "authenticated":
        assert state.logged_in is True
    else:
        assert state.logged_in is False
    assert isinstance(state.to_dict(), dict)


# ── standalone runner ───────────────────────────────────────────────────────

def _run_one(name):
    fn = globals()[name]
    try:
        if name == "test_detect_live_adapter" and not _live_adapter():
            print(f"{SKIP} {name} (no connected web adapter)")
            return True
        fn()
        print(f"{PASS} {name}")
        return True
    except Exception as exc:  # noqa: BLE001 — standalone reporter
        print(f"{FAIL} {name}: {exc!r}")
        return False


def main() -> int:
    tests = [n for n in globals() if n.startswith("test_")]
    ok = all(_run_one(n) for n in tests)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
