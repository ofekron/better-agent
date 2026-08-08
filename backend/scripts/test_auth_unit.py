#!/usr/bin/env python3
"""Dedicated unit coverage for backend/auth.py.

auth.py is the password-verification + bearer-token + login rate-limit gate.
The route-level tests (test_auth_routes, test_auth_me_bearer_native, ...)
exercise it end-to-end but leave several direct branches cold (~84%): the
bootstrap `_load` paths, `reload_credentials`, the bearer/access token
round-trips, the no-token and length-mismatch helpers, the
not-bootstrapped tarpit, and the rate-limiter's per-IP stale-pop loop.
This file drives each branch directly with real assertions.

State isolation: a throwaway BETTER_AGENT_HOME via `paths.engage_test_home`
before any backend import, and every `auth` module global / collaborator
patch is reverted via the `monkeypatch` fixture so the module is left clean
for the rest of the suite.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from collections import defaultdict, deque
from pathlib import Path
from types import SimpleNamespace

import pytest

_TEST_HOME = tempfile.mkdtemp(prefix="ba-auth-unit-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths  # noqa: E402

paths.engage_test_home(_TEST_HOME)

import auth  # noqa: E402
import auth_secrets  # noqa: E402

# test_auth_change_credentials.py rebinds `auth.reload_credentials` to a fake
# at ITS import time (module-level, no teardown), and pytest imports every
# collected module before running any test — so by the time this file runs,
# `auth.reload_credentials` may already be the fake. To exercise the genuine
# reload branch we load a pristine copy of auth.py; coverage still attributes
# its execution to backend/auth.py (same source file).
import importlib.util  # noqa: E402

_AUTH_SRC = Path(auth.__file__)
_SPEC = importlib.util.spec_from_file_location("_auth_pristine", _AUTH_SRC)
auth_pristine = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(auth_pristine)


# ---------------------------------------------------------------------------
# _load / bootstrap
# ---------------------------------------------------------------------------


def test_is_bootstrapped_reflects_flag(monkeypatch):
    monkeypatch.setattr(auth, "_BOOTSTRAPPED", False)
    assert auth.is_bootstrapped() is False
    monkeypatch.setattr(auth, "_BOOTSTRAPPED", True)
    assert auth.is_bootstrapped() is True


def test_load_success_returns_credentials(monkeypatch):
    h = auth_secrets.make_password_hash("pw")
    monkeypatch.setattr(auth.auth_secrets, "read_all_parallel", lambda: ("alice", h, "s3cr3t"))
    assert auth._load() == ("alice", h, "s3cr3t", True)


def test_load_missing_credentials_reports_not_bootstrapped(monkeypatch):
    def _boom():
        raise RuntimeError("no keychain")
    monkeypatch.setattr(auth.auth_secrets, "read_all_parallel", _boom)
    monkeypatch.setattr(auth.auth_secrets, "headless_mode_enabled", lambda: False)
    assert auth._load() == (None, None, None, False)


def test_load_reraises_in_headless_mode(monkeypatch):
    """Headless containers have no setup screen, so a missing credential must
    crash loudly at import instead of degrading into an un-completable UI."""
    def _boom():
        raise RuntimeError("misconfigured")
    monkeypatch.setattr(auth.auth_secrets, "read_all_parallel", _boom)
    monkeypatch.setattr(auth.auth_secrets, "headless_mode_enabled", lambda: True)
    with pytest.raises(RuntimeError):
        auth._load()


def test_reload_credentials_flips_to_bootstrapped(monkeypatch):
    # Run against the pristine auth module: another test file fakes
    # `auth.reload_credentials` for the whole session (see module docstring).
    h = auth_secrets.make_password_hash("pw")
    monkeypatch.setattr(auth_pristine.auth_secrets, "read_all_parallel", lambda: ("alice", h, "sek"))
    auth_pristine.reload_credentials()
    assert auth_pristine.is_bootstrapped() is True
    assert auth_pristine.current_username() == "alice"
    assert auth_pristine.get_session_secret() == "sek"


def test_current_username_none_pre_bootstrap(monkeypatch):
    monkeypatch.setattr(auth, "_USERNAME", None)
    assert auth.current_username() is None


def test_get_session_secret_uses_ephemeral_pre_bootstrap(monkeypatch):
    monkeypatch.setattr(auth, "_BOOTSTRAPPED", False)
    # The ephemeral secret is stable for the process, and must NOT be the real
    # (absent) session secret — it exists only so SessionMiddleware can sign.
    assert auth.get_session_secret() == auth._EPHEMERAL_SECRET


# ---------------------------------------------------------------------------
# Bearer + access tokens
# ---------------------------------------------------------------------------


def test_password_bearer_token_roundtrip(monkeypatch):
    monkeypatch.setattr(auth, "_BOOTSTRAPPED", False)  # -> ephemeral signing key
    token = auth.create_token("alice")
    assert isinstance(token, str)
    assert auth.verify_token(token) == {"username": "alice"}


def test_access_token_roundtrip_and_short_lived(monkeypatch):
    monkeypatch.setattr(auth, "_BOOTSTRAPPED", False)
    token = auth.create_access_token("bob")
    assert auth.verify_token(token) == {"username": "bob"}


def test_verify_token_rejects_empty_and_none(monkeypatch):
    assert auth.verify_token(None) is None
    assert auth.verify_token("") is None


def test_verify_token_rejects_garbage(monkeypatch):
    monkeypatch.setattr(auth, "_BOOTSTRAPPED", False)
    assert auth.verify_token("not-a-real-token") is None


def test_verify_token_rejects_unsigned_payload(monkeypatch):
    """A valid token signed under a different secret must not validate once the
    real secret loads — credential rotation kills outstanding tokens."""
    monkeypatch.setattr(auth, "_BOOTSTRAPPED", False)
    forged_secret = auth._EPHEMERAL_SECRET + "x"
    from itsdangerous import URLSafeTimedSerializer

    forged = URLSafeTimedSerializer(forged_secret, salt=auth._TOKEN_NAMESPACE).dumps({"username": "x"})
    assert auth.verify_token(forged) is None


def test_verify_token_ignores_payload_without_username(monkeypatch):
    """A token that decodes cleanly but carries no `username` (or isn't a dict)
    must fall through both serializers and be rejected — the gate keys on the
    username claim, not mere signature validity."""
    monkeypatch.setattr(auth, "_BOOTSTRAPPED", False)
    from itsdangerous import URLSafeTimedSerializer

    key = auth.get_session_secret()
    no_username = URLSafeTimedSerializer(key, salt=auth._TOKEN_NAMESPACE).dumps({"not_user": 1})
    plain = URLSafeTimedSerializer(key, salt=auth._TOKEN_NAMESPACE).dumps("just-a-string")
    assert auth.verify_token(no_username) is None
    assert auth.verify_token(plain) is None


# ---------------------------------------------------------------------------
# identify_request — session user first, bearer identity as fallback
# ---------------------------------------------------------------------------

_NO_BEARER = object()


class _FakeRequest:
    """Minimal stand-in for a Starlette Request over only the two attributes
    identify_request reads: `scope` (membership-tested for "session") and
    `session` (a dict with `.get("user")`). `state.bearer_user` is set only
    when the auth gate resolved a bearer identity for the request."""

    def __init__(self, session=None, session_in_scope=True, bearer_user=_NO_BEARER):
        self.scope = {"session": session} if session_in_scope else {}
        self.session = session if session is not None else {}
        self.state = (
            SimpleNamespace()
            if bearer_user is _NO_BEARER
            else SimpleNamespace(bearer_user=bearer_user)
        )


def test_identify_request_prefers_session_user():
    user = {"username": "alice"}
    req = _FakeRequest(session={"user": user})
    assert auth.identify_request(req) is user


def test_identify_request_falls_back_to_bearer_when_no_session_user():
    bearer = {"username": "bob"}
    # Session is in scope but carries no user -> bearer identity wins.
    req = _FakeRequest(session={}, bearer_user=bearer)
    assert auth.identify_request(req) is bearer


def test_identify_request_uses_bearer_when_no_session_in_scope():
    bearer = {"username": "carol"}
    req = _FakeRequest(session_in_scope=False, bearer_user=bearer)
    assert auth.identify_request(req) is bearer


def test_identify_request_returns_none_without_any_identity():
    # No session user and no resolved bearer -> honest anonymous answer.
    req = _FakeRequest(session={})
    assert auth.identify_request(req) is None


# ---------------------------------------------------------------------------
# Constant-time username compare
# ---------------------------------------------------------------------------


def test_const_eq_equal_strings():
    assert auth._const_eq("alice", "alice") is True


def test_const_eq_different_same_length():
    assert auth._const_eq("alice", "alica") is False


def test_const_eq_different_length():
    # Length-mismatch branch still walks the padded pair and must return False.
    assert auth._const_eq("ab", "abc") is False
    assert auth._const_eq("longer", "x") is False


# ---------------------------------------------------------------------------
# verify_credentials
# ---------------------------------------------------------------------------


class _StubAsyncio:
    """auth.verify_credentials only awaits `asyncio.sleep`; stub it so the
    200ms tarpit doesn't slow the suite."""

    @staticmethod
    async def sleep(_seconds):
        return None


def _run(coro):
    return asyncio.run(coro)


def test_verify_credentials_rejects_when_not_bootstrapped(monkeypatch):
    monkeypatch.setattr(auth, "_BOOTSTRAPPED", False)
    monkeypatch.setattr(auth, "asyncio", _StubAsyncio)
    assert _run(auth.verify_credentials("alice", "pw")) is False


def test_verify_credentials_success(monkeypatch):
    h = auth_secrets.make_password_hash("s3cret")
    monkeypatch.setattr(auth, "_BOOTSTRAPPED", True)
    monkeypatch.setattr(auth, "_USERNAME", "alice")
    monkeypatch.setattr(auth, "_PASSWORD_HASH", h)
    monkeypatch.setattr(auth, "asyncio", _StubAsyncio)
    assert _run(auth.verify_credentials("alice", "s3cret")) is True


def test_verify_credentials_wrong_password(monkeypatch):
    h = auth_secrets.make_password_hash("s3cret")
    monkeypatch.setattr(auth, "_BOOTSTRAPPED", True)
    monkeypatch.setattr(auth, "_USERNAME", "alice")
    monkeypatch.setattr(auth, "_PASSWORD_HASH", h)
    monkeypatch.setattr(auth, "asyncio", _StubAsyncio)
    assert _run(auth.verify_credentials("alice", "wrong")) is False


def test_verify_credentials_wrong_username(monkeypatch):
    h = auth_secrets.make_password_hash("s3cret")
    monkeypatch.setattr(auth, "_BOOTSTRAPPED", True)
    monkeypatch.setattr(auth, "_USERNAME", "alice")
    monkeypatch.setattr(auth, "_PASSWORD_HASH", h)
    monkeypatch.setattr(auth, "asyncio", _StubAsyncio)
    # Correct password, wrong username — must still fail (no enumeration).
    assert _run(auth.verify_credentials("bob", "s3cret")) is False


# ---------------------------------------------------------------------------
# Rate limiter — the per-IP stale-pop loop (dq.popleft)
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_rate_limit_pops_stale_attempts_for_same_ip(monkeypatch):
    """The `while dq and dq[0] < cutoff` loop pops only the aged HEAD of an
    IP's deque when a newer attempt in the same deque is still fresh. To reach
    it, the deque must hold a stale head AND a fresh tail (else the
    once-per-window prune drops the whole entry first)."""
    clock = _FakeClock(10_000.0)
    monkeypatch.setattr(auth, "time", type("_T", (), {"monotonic": clock}))
    monkeypatch.setattr(auth, "_rl_attempts", defaultdict(deque))
    monkeypatch.setattr(auth, "_rl_last_sweep", 0.0)
    ip = "203.0.113.7"
    # Two attempts within the window: head @10000, tail @10300.
    assert auth.rate_limit_check(ip) is True
    clock.t = 10_300.0
    assert auth.rate_limit_check(ip) is True
    assert list(auth._rl_attempts[ip]) == [10_000.0, 10_300.0]
    # Advance so the head is stale (10000 < 10100) but the tail is fresh
    # (10300 >= 10100): the prune keeps the entry, the while-loop pops the
    # stale head, and the IP is re-allowed with one surviving attempt.
    clock.t = 10_400.0
    assert auth.rate_limit_check(ip) is True
    assert auth._rl_attempts[ip][0] == 10_300.0
    assert auth._rl_attempts[ip][-1] == 10_400.0


def test_rate_limit_prune_drops_stale_and_empty_entries(monkeypatch):
    """The once-per-window sweep (`_rl_prune_stale`) must drop IPs whose last
    attempt aged out (dq[-1] < cutoff) AND empty deques, while leaving a fresh
    entry untouched. Seed all three, force the sweep, assert only the fresh
    entry survives."""
    clock = _FakeClock(10_000.0)
    monkeypatch.setattr(auth, "time", type("_T", (), {"monotonic": clock}))
    monkeypatch.setattr(auth, "_rl_attempts", defaultdict(deque))
    monkeypatch.setattr(auth, "_rl_last_sweep", 0.0)  # now - 0 >= window -> sweep
    # stale: last attempt @9500 is older than cutoff (10000-300=9700)
    auth._rl_attempts["stale-ip"].append(9_500.0)
    # empty deque (an entry fully popped on a prior check)
    _ = auth._rl_attempts["empty-ip"]
    # fresh: last attempt within the window -> must survive
    auth._rl_attempts["fresh-ip"].append(9_800.0)
    assert auth.rate_limit_check("caller-ip") is True
    assert "stale-ip" not in auth._rl_attempts
    assert "empty-ip" not in auth._rl_attempts
    assert "fresh-ip" in auth._rl_attempts


def test_rate_limit_locks_out_after_max_within_window(monkeypatch):
    clock = _FakeClock(10_000.0)
    monkeypatch.setattr(auth, "time", type("_T", (), {"monotonic": clock}))
    monkeypatch.setattr(auth, "_rl_attempts", defaultdict(deque))
    monkeypatch.setattr(auth, "_rl_last_sweep", clock())  # no mid-test sweep
    ip = "198.51.100.5"
    for _ in range(auth._RL_MAX):
        assert auth.rate_limit_check(ip) is True
    # One past the budget -> refused, and the deque does not keep growing.
    assert auth.rate_limit_check(ip) is False
    assert len(auth._rl_attempts[ip]) == auth._RL_MAX


def test_rate_limit_reset_clears_ip_history(monkeypatch):
    clock = _FakeClock(10_000.0)
    monkeypatch.setattr(auth, "time", type("_T", (), {"monotonic": clock}))
    monkeypatch.setattr(auth, "_rl_attempts", defaultdict(deque))
    monkeypatch.setattr(auth, "_rl_last_sweep", clock())
    ip = "198.51.100.9"
    auth.rate_limit_check(ip)
    assert ip in auth._rl_attempts
    auth.rate_limit_reset(ip)
    assert ip not in auth._rl_attempts


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
