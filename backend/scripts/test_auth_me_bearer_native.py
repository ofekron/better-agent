"""Regression: `/api/auth/me` must accept the bearer token, not just the
session cookie.

Root cause it locks: the auth-gate middleware used to exempt everything
under `/api/auth/`, including `/me`. So the bearer-token fallback (the
only auth that works cross-origin) never ran for `/me`, and the `/me`
handler only read the session cookie. On the native Capacitor build the
WebView origin (http://localhost) differs from the backend (LAN IP), so
the SameSite=Lax cookie is dropped — `/me` 401s and the just-logged-in
mobile user bounces back to <Login /> with no error shown.

Checks (no cookie on the client, no test bypass):
  1. /api/auth/me + valid bearer   -> 200, returns the token's username
  2. /api/auth/me + no credentials -> 401
  3. /api/auth/me + bogus bearer   -> 401

Fails before the fix: case 1 returns 401 (bearer ignored on /me).

Run with:
    cd backend && .venv/bin/python scripts/test_auth_me_bearer_native.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-authme-")
# Real auth path only — never the loopback test bypass.
os.environ.pop("BETTER_CLAUDE_TEST_AUTH_BYPASS", None)

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from starlette.testclient import TestClient  # noqa: E402
import auth  # noqa: E402
import main  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_USERNAME = "mobile-native-user"
_TOKEN = auth.create_token(_USERNAME)


def _fresh_client() -> TestClient:
    # A brand-new client carries NO session cookie — models a native
    # Capacitor client whose SameSite=Lax cookie can't cross origins.
    # One per case so the valid-bearer case can't leak its session
    # cookie to the rejection cases.
    return TestClient(main.app, client=("127.0.0.1", 50000))


def test_valid_bearer_authenticates() -> tuple[bool, str]:
    """Case 1: a valid bearer token (no cookie) authenticates /me."""
    res = _fresh_client().get(
        "/api/auth/me", headers={"Authorization": f"Bearer {_TOKEN}"}
    )
    if res.status_code != 200:
        return False, f"expected 200, got {res.status_code}"
    body = res.json()
    if body.get("username") != _USERNAME:
        return False, f"expected username {_USERNAME!r}, got {body!r}"
    return True, ""


def test_no_credentials_rejected() -> tuple[bool, str]:
    """Case 2: no cookie, no bearer -> 401."""
    res = _fresh_client().get("/api/auth/me")
    return res.status_code == 401, f"expected 401, got {res.status_code}"


def test_bogus_bearer_rejected() -> tuple[bool, str]:
    """Case 3: a forged token -> 401 (signature must be verified)."""
    res = _fresh_client().get(
        "/api/auth/me", headers={"Authorization": "Bearer not.a.real.token"}
    )
    return res.status_code == 401, f"expected 401, got {res.status_code}"


TESTS = [
    ("valid bearer token authenticates /me (native cross-origin login)",
     test_valid_bearer_authenticates),
    ("no credentials -> 401", test_no_credentials_rejected),
    ("bogus bearer -> 401", test_bogus_bearer_rejected),
]


def main_run() -> int:
    failed = 0
    for name, fn in TESTS:
        try:
            ok, detail = fn()
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"exception: {e}"
            import traceback
            traceback.print_exc()
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' — ' + detail}")
        if not ok:
            failed += 1
    print()
    print(f"{failed} of {len(TESTS)} test(s) FAILED" if failed
          else f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
