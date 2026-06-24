"""Test backend/auth_secrets.py — first-run credential bootstrap.

The keychain round-trip runs under a UNIQUE throwaway service name, so
the user's real `better-claude` keychain entries are never read or
written, and the test entries are deleted on exit.

Run with:
    cd backend && .venv/bin/python scripts/test_auth_bootstrap.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import auth_secrets  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_TEST_SERVICE = f"better-agent-test-{uuid.uuid4().hex[:12]}"
_TEST_LEGACY_SERVICE = f"better-claude-test-{uuid.uuid4().hex[:12]}"


def test_password_hash_verifies() -> bool:
    """`make_password_hash` produces an argon2 hash that verifies against
    the original password."""
    import argon2
    h = auth_secrets.make_password_hash("hunter2")
    try:
        argon2.PasswordHasher().verify(h, "hunter2")
    except Exception as e:
        print(f"  hash did not verify: {e}")
        return False
    return True


def test_write_credentials_rejects_empty() -> bool:
    """`write_credentials` raises on an empty username or password."""
    for username, password in (("", "x"), ("x", "")):
        try:
            auth_secrets.write_credentials(username, password)
        except ValueError:
            continue
        print(f"  expected ValueError for ({username!r}, {password!r})")
        return False
    return True


def test_keychain_roundtrip() -> bool:
    """needs_bootstrap → write_credentials → read-back, all under a
    throwaway keychain service (real `better-claude` entries untouched)."""
    real_service = auth_secrets._SERVICE
    real_legacy_service = auth_secrets._LEGACY_SERVICE
    auth_secrets._SERVICE = _TEST_SERVICE
    auth_secrets._LEGACY_SERVICE = _TEST_LEGACY_SERVICE
    try:
        if not auth_secrets.needs_bootstrap():
            print("  a fresh service should report needs_bootstrap=True")
            return False
        auth_secrets.write_credentials("alice", "s3cret-pw")
        if auth_secrets.needs_bootstrap():
            print("  needs_bootstrap still True after write_credentials")
            return False
        if auth_secrets.get_username() != "alice":
            print(f"  username round-trip failed: {auth_secrets.get_username()!r}")
            return False
        import argon2
        try:
            argon2.PasswordHasher().verify(
                auth_secrets.get_password_hash(), "s3cret-pw",
            )
        except Exception as e:
            print(f"  stored password hash did not verify: {e}")
            return False
        if len(auth_secrets.get_session_secret()) != 64:
            print("  session_secret is not 64 hex chars")
            return False
        for account in ("username", "password_hash", "session_secret"):
            if not _keychain_has(_TEST_LEGACY_SERVICE, account):
                print(f"  legacy service did not receive {account}")
                return False
        return True
    finally:
        auth_secrets._SERVICE = real_service
        auth_secrets._LEGACY_SERVICE = real_legacy_service
        _delete_test_services()


def test_legacy_keychain_fallback() -> bool:
    real_service = auth_secrets._SERVICE
    real_legacy_service = auth_secrets._LEGACY_SERVICE
    auth_secrets._SERVICE = _TEST_SERVICE
    auth_secrets._LEGACY_SERVICE = _TEST_LEGACY_SERVICE
    try:
        _delete_test_services()
        _security_store(_TEST_LEGACY_SERVICE, "username", "legacy-alice")
        _security_store(_TEST_LEGACY_SERVICE, "password_hash", auth_secrets.make_password_hash("legacy-pw"))
        _security_store(_TEST_LEGACY_SERVICE, "session_secret", "a" * 64)
        if auth_secrets.needs_bootstrap():
            print("  complete legacy service should not need bootstrap")
            return False
        if auth_secrets.get_username() != "legacy-alice":
            print("  username did not fall back to legacy service")
            return False
        return True
    finally:
        auth_secrets._SERVICE = real_service
        auth_secrets._LEGACY_SERVICE = real_legacy_service
        _delete_test_services()


def _security_store(service: str, account: str, value: str) -> None:
    subprocess.run(
        ["/usr/bin/security", "add-generic-password", "-U", "-s", service, "-a", account, "-w", value],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _keychain_has(service: str, account: str) -> bool:
    return subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def _delete_test_services() -> None:
    for service in (_TEST_SERVICE, _TEST_LEGACY_SERVICE):
        for account in ("username", "password_hash", "session_secret"):
            subprocess.run(
                ["/usr/bin/security", "delete-generic-password",
                 "-s", service, "-a", account],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )


TESTS = [
    ("make_password_hash produces a verifiable argon2 hash",
     test_password_hash_verifies),
    ("write_credentials rejects empty username/password",
     test_write_credentials_rejects_empty),
    ("keychain bootstrap round-trips under a throwaway service",
     test_keychain_roundtrip),
    ("legacy keychain service remains readable",
     test_legacy_keychain_fallback),
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
