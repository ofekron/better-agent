"""Regression: a bearer-only request must not "resurrect" a session cookie.

Root cause it locks: the auth_gate middleware's bearer-token fallback used
to write the resolved identity into `request.session`. SessionMiddleware
then re-issues a Set-Cookie for that now-populated session on the
response — even for a request whose cookie was empty or absent, e.g. one
made right after `POST /api/auth/logout`. A client that (like the real
frontend) attaches `Authorization: Bearer <token>` to every fetch site-wide
would silently regain a persistent, cookie-only-authenticated session on
its very next request, even one that carries no bearer token at all. Logout
via the API therefore did not actually end the cookie-based session as
long as a bearer token was still being sent alongside it.

Checks (no cookie on the client, no test bypass, real bearer verification):
  1. A bearer-only request to /api/auth/me must not plant a session cookie.
  2. A client that drops the bearer header after such a request must stay
     unauthenticated (no resurrected cookie session) — 401, not 200.

Fails before the fix: case 1 finds a session cookie was set; case 2 gets
200 instead of 401.

Run with:
    cd backend && .venv/bin/python scripts/test_auth_logout_no_bearer_resurrection.py
"""

from __future__ import annotations

import os
import shutil
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-noresurrect-")
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

_USERNAME = "bearer-resurrection-user"
_TOKEN = auth.create_token(_USERNAME)
_SESSION_COOKIE = "better_agent_session"


def _fresh_client() -> TestClient:
    return TestClient(main.app, client=("127.0.0.1", 50001))


def test_bearer_only_request_does_not_plant_session_cookie() -> tuple[bool, str]:
    """Case 1: authenticating via bearer alone must not set a session cookie."""
    client = _fresh_client()
    res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {_TOKEN}"})
    if res.status_code != 200:
        return False, f"expected 200, got {res.status_code}"
    if _SESSION_COOKIE in client.cookies:
        return False, "a bearer-only request planted a session cookie (resurrection)"
    return True, ""


def test_dropping_bearer_after_prior_request_stays_unauthenticated() -> tuple[bool, str]:
    """Case 2: once the bearer header is dropped, a client that only carries
    whatever cookie a prior bearer-authenticated request left behind must
    NOT be authenticated — mirrors calling /api/auth/logout (clears the
    session) then making a follow-up request without re-attaching the
    bearer header."""
    client = _fresh_client()
    client.get("/api/auth/me", headers={"Authorization": f"Bearer {_TOKEN}"})
    res = client.get("/api/auth/me")
    return res.status_code == 401, f"expected 401, got {res.status_code}"


TESTS = [
    ("bearer-only request does not plant a session cookie",
     test_bearer_only_request_does_not_plant_session_cookie),
    ("dropping the bearer header afterward stays unauthenticated",
     test_dropping_bearer_after_prior_request_stays_unauthenticated),
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
