"""Test backend/auth_secrets.py — headless container credential mode.

BETTER_AGENT_HEADLESS_AUTH=1 sources credentials from env vars / mounted
secret files instead of an OS keychain, for running the backend in a
Linux container with no D-Bus / Secret Service daemon. All env vars this
module touches are saved and restored so the real process environment
(and any real OS keychain) is never affected.

Run with:
    cd backend && .venv/bin/python scripts/test_auth_secrets_headless.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import auth_secrets  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_ENV_VARS = (
    "BETTER_AGENT_HEADLESS_AUTH",
    "BETTER_AGENT_USERNAME",
    "BETTER_AGENT_PASSWORD_HASH_FILE",
    "BETTER_AGENT_SESSION_SECRET_FILE",
)


@contextmanager
def _clean_env():
    saved = {var: os.environ.get(var) for var in _ENV_VARS}
    for var in _ENV_VARS:
        os.environ.pop(var, None)
    auth_secrets._headless_ephemeral_session_secret = None
    try:
        yield
    finally:
        for var, value in saved.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value
        auth_secrets._headless_ephemeral_session_secret = None


def test_disabled_by_default() -> bool:
    with _clean_env():
        if auth_secrets.headless_mode_enabled():
            print("  headless mode should be off with no env vars set")
            return False
        return True


def test_missing_username_raises() -> bool:
    with _clean_env():
        os.environ["BETTER_AGENT_HEADLESS_AUTH"] = "1"
        try:
            auth_secrets.get_username()
        except RuntimeError:
            return True
        print("  expected RuntimeError for missing BETTER_AGENT_USERNAME")
        return False


def test_missing_password_hash_file_raises() -> bool:
    with _clean_env():
        os.environ["BETTER_AGENT_HEADLESS_AUTH"] = "1"
        os.environ["BETTER_AGENT_USERNAME"] = "alice"
        try:
            auth_secrets.get_password_hash()
        except RuntimeError:
            return True
        print("  expected RuntimeError for missing BETTER_AGENT_PASSWORD_HASH_FILE")
        return False


def test_reads_username_and_password_hash() -> bool:
    with _clean_env(), tempfile.TemporaryDirectory() as tmp:
        expected_hash = auth_secrets.make_password_hash("hunter2")
        hash_path = os.path.join(tmp, "password_hash")
        with open(hash_path, "w", encoding="utf-8") as handle:
            handle.write(expected_hash + "\n")

        os.environ["BETTER_AGENT_HEADLESS_AUTH"] = "1"
        os.environ["BETTER_AGENT_USERNAME"] = "alice"
        os.environ["BETTER_AGENT_PASSWORD_HASH_FILE"] = hash_path

        if auth_secrets.get_username() != "alice":
            print(f"  username mismatch: {auth_secrets.get_username()!r}")
            return False
        if auth_secrets.get_password_hash() != expected_hash:
            print("  password hash did not round-trip through the file")
            return False

        import argon2
        try:
            argon2.PasswordHasher().verify(auth_secrets.get_password_hash(), "hunter2")
        except Exception as e:
            print(f"  stored hash did not verify: {e}")
            return False
        return True


def test_session_secret_from_file() -> bool:
    with _clean_env(), tempfile.TemporaryDirectory() as tmp:
        secret_path = os.path.join(tmp, "session_secret")
        with open(secret_path, "w", encoding="utf-8") as handle:
            handle.write("b" * 64)

        os.environ["BETTER_AGENT_HEADLESS_AUTH"] = "1"
        os.environ["BETTER_AGENT_SESSION_SECRET_FILE"] = secret_path

        if auth_secrets.get_session_secret() != "b" * 64:
            print("  session secret did not round-trip through the file")
            return False
        return True


def test_session_secret_ephemeral_when_unset() -> bool:
    with _clean_env():
        os.environ["BETTER_AGENT_HEADLESS_AUTH"] = "1"
        first = auth_secrets.get_session_secret()
        second = auth_secrets.get_session_secret()
        if len(first) != 64:
            print(f"  ephemeral session secret is not 64 hex chars: {first!r}")
            return False
        if first != second:
            print("  ephemeral session secret changed between calls in the same process")
            return False
        return True


def test_needs_bootstrap_false_in_headless_mode() -> bool:
    with _clean_env():
        os.environ["BETTER_AGENT_HEADLESS_AUTH"] = "1"
        if auth_secrets.needs_bootstrap():
            print("  needs_bootstrap() should be False in headless mode")
            return False
        return True


def test_write_credentials_rejected_in_headless_mode() -> bool:
    with _clean_env():
        os.environ["BETTER_AGENT_HEADLESS_AUTH"] = "1"
        for fn in (auth_secrets.write_credentials, auth_secrets.write_login_credentials):
            try:
                fn("alice", "s3cret-pw")
            except RuntimeError:
                continue
            print(f"  expected RuntimeError from {fn.__name__} in headless mode")
            return False
        return True


TESTS = [
    ("headless mode is off by default", test_disabled_by_default),
    ("missing BETTER_AGENT_USERNAME raises", test_missing_username_raises),
    ("missing BETTER_AGENT_PASSWORD_HASH_FILE raises", test_missing_password_hash_file_raises),
    ("username + password hash read from env/file", test_reads_username_and_password_hash),
    ("session secret read from mounted file", test_session_secret_from_file),
    ("session secret falls back to a stable per-process ephemeral value", test_session_secret_ephemeral_when_unset),
    ("needs_bootstrap() is False in headless mode", test_needs_bootstrap_false_in_headless_mode),
    ("write_credentials/write_login_credentials rejected in headless mode", test_write_credentials_rejected_in_headless_mode),
]


def main_run() -> int:
    failed = 0
    for name, fn in TESTS:
        try:
            ok = fn()
        except Exception as e:
            ok = False
            import traceback
            traceback.print_exc()
            print(f"  exception: {e}")
        print(f"{PASS if ok else FAIL}  {name}")
        if not ok:
            failed += 1
    print()
    print(f"{failed} of {len(TESTS)} test(s) FAILED" if failed
          else f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
