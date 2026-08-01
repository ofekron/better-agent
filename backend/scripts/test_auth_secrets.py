"""Unit tests for auth_secrets — macOS-keychain / headless-file auth reads
and the first-run credential bootstrap (write side).

The module is platform-bound (darwin shells out to /usr/bin/security;
non-darwin uses the python keyring package). We never touch the real
keychain: subprocess.check_output / subprocess.run are monkeypatched,
oskeychain.store is recorded, and keyring.get_password is patched for the
non-darwin branches (driven by monkeypatching sys.platform). Headless
file-backed reads use real files under tmp_path. All deterministic ->
unit tier.
"""
from __future__ import annotations

import os
import subprocess
import sys

import _test_home  # noqa: F401  (engages prod-home guard before backend import)

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest

import auth_secrets  # noqa: E402


# --------------------------------------------------------------------------- #
# shared helpers / fixtures
# --------------------------------------------------------------------------- #
def _argv_val(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


@pytest.fixture(autouse=True)
def _reset_headless_state(monkeypatch):
    """Each test starts with headless disabled and no cached ephemeral secret."""
    monkeypatch.delenv("BETTER_AGENT_HEADLESS_AUTH", raising=False)
    monkeypatch.delenv("BETTER_AGENT_USERNAME", raising=False)
    monkeypatch.delenv("BETTER_AGENT_PASSWORD_HASH_FILE", raising=False)
    monkeypatch.delenv("BETTER_AGENT_SESSION_SECRET_FILE", raising=False)
    monkeypatch.setattr(auth_secrets, "_headless_ephemeral_session_secret", None)


def _check_output_table(table):
    """Build a check_output stub keyed by (service, account).

    table[(service, account)] is either a str (returned, with trailing
    newline to mimic `security -w`) or an Exception to raise.
    """

    def _fake(cmd, *args, **kwargs):
        service = _argv_val(cmd, "-s")
        account = _argv_val(cmd, "-a")
        entry = table.get((service, account))
        if isinstance(entry, Exception):
            raise entry
        return entry

    return _fake


# --------------------------------------------------------------------------- #
# headless_mode_enabled
# --------------------------------------------------------------------------- #
def test_headless_mode_enabled_false():
    assert auth_secrets.headless_mode_enabled() is False


def test_headless_mode_enabled_true(monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_HEADLESS_AUTH", "1")
    assert auth_secrets.headless_mode_enabled() is True


# --------------------------------------------------------------------------- #
# _read_secret_file
# --------------------------------------------------------------------------- #
def test_read_secret_file_unset_var():
    with pytest.raises(RuntimeError, match="PASSWORD_HASH_FILE"):
        auth_secrets._read_secret_file("BETTER_AGENT_PASSWORD_HASH_FILE")


def test_read_secret_file_unreadable(monkeypatch, tmp_path):
    # Pointing at a directory raises OSError on read.
    monkeypatch.setenv("BETTER_AGENT_PASSWORD_HASH_FILE", str(tmp_path))
    with pytest.raises(RuntimeError, match="could not be read"):
        auth_secrets._read_secret_file("BETTER_AGENT_PASSWORD_HASH_FILE")


def test_read_secret_file_empty(monkeypatch, tmp_path):
    f = tmp_path / "empty"
    f.write_text("   \n")
    monkeypatch.setenv("BETTER_AGENT_PASSWORD_HASH_FILE", str(f))
    with pytest.raises(RuntimeError, match="is empty"):
        auth_secrets._read_secret_file("BETTER_AGENT_PASSWORD_HASH_FILE")


def test_read_secret_file_strips_value(monkeypatch, tmp_path):
    f = tmp_path / "secret"
    f.write_text("  hunter2\n")
    monkeypatch.setenv("BETTER_AGENT_PASSWORD_HASH_FILE", str(f))
    assert auth_secrets._read_secret_file("BETTER_AGENT_PASSWORD_HASH_FILE") == "hunter2"


# --------------------------------------------------------------------------- #
# headless getters
# --------------------------------------------------------------------------- #
def test_headless_get_username_missing():
    with pytest.raises(RuntimeError, match="BETTER_AGENT_USERNAME"):
        auth_secrets._headless_get_username()


def test_headless_get_username_ok(monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_USERNAME", "ofek")
    assert auth_secrets._headless_get_username() == "ofek"


def test_headless_get_password_hash_delegates(monkeypatch, tmp_path):
    f = tmp_path / "hash"
    f.write_text("$argon2id$hash\n")
    monkeypatch.setenv("BETTER_AGENT_PASSWORD_HASH_FILE", str(f))
    assert auth_secrets._headless_get_password_hash() == "$argon2id$hash"


def test_headless_get_session_secret_from_file(monkeypatch, tmp_path):
    f = tmp_path / "secret"
    f.write_text("deadbeef\n")
    monkeypatch.setenv("BETTER_AGENT_SESSION_SECRET_FILE", str(f))
    assert auth_secrets._headless_get_session_secret() == "deadbeef"


def test_headless_get_session_secret_ephemeral_then_cached(monkeypatch, capsys):
    monkeypatch.delenv("BETTER_AGENT_SESSION_SECRET_FILE", raising=False)
    first = auth_secrets._headless_get_session_secret()
    second = auth_secrets._headless_get_session_secret()
    assert first == second
    assert len(first) == 64  # token_hex(32) -> 64 chars
    assert "ephemeral session secret" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# _kc_get (darwin path)
# --------------------------------------------------------------------------- #
def test_kc_get_darwin_happy(monkeypatch):
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: "value\n")
    assert auth_secrets._kc_get("better-agent", "username") == "value"


def test_kc_get_darwin_timeout(monkeypatch):
    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="security", timeout=5)

    monkeypatch.setattr(subprocess, "check_output", _raise)
    with pytest.raises(RuntimeError, match="Keychain locked"):
        auth_secrets._kc_get("better-agent", "username")


def test_kc_get_darwin_missing(monkeypatch):
    def _raise(*a, **k):
        raise subprocess.CalledProcessError(returncode=44, cmd="security")

    monkeypatch.setattr(subprocess, "check_output", _raise)
    with pytest.raises(RuntimeError, match="Keychain entry missing"):
        auth_secrets._kc_get("better-agent", "username")


def test_kc_get_non_darwin_keyring_value(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("keyring.get_password", lambda service, account: "  kw\n")
    assert auth_secrets._kc_get("better-agent", "username") == "kw"


def test_kc_get_non_darwin_keychain_missing(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("keyring.get_password", lambda service, account: None)
    with pytest.raises(RuntimeError, match="Credential entry missing"):
        auth_secrets._kc_get("better-agent", "username")


# --------------------------------------------------------------------------- #
# _kc (multi-service loop)
# --------------------------------------------------------------------------- #
def test_kc_returns_first_success(monkeypatch):
    services = auth_secrets._services()
    monkeypatch.setattr(
        subprocess, "check_output", _check_output_table({(services[0], "username"): "u1\n"})
    )
    assert auth_secrets._kc("username") == "u1"


def test_kc_falls_through_to_next_service(monkeypatch):
    services = auth_secrets._services()
    table = {
        (services[0], "username"): subprocess.CalledProcessError(44, "security"),
        (services[1], "username"): "u2\n",
    }
    monkeypatch.setattr(subprocess, "check_output", _check_output_table(table))
    assert auth_secrets._kc("username") == "u2"


def test_kc_all_services_fail_raises_last(monkeypatch):
    services = auth_secrets._services()
    table = {(s, "username"): subprocess.CalledProcessError(44, "security") for s in services}
    monkeypatch.setattr(subprocess, "check_output", _check_output_table(table))
    with pytest.raises(RuntimeError, match="Keychain entry missing"):
        auth_secrets._kc("username")


# --------------------------------------------------------------------------- #
# get_username / get_password_hash / get_session_secret
# --------------------------------------------------------------------------- #
def _patch_all_accounts(monkeypatch, values):
    table = {}
    for service in auth_secrets._services():
        for account, val in values.items():
            table[(service, account)] = val + "\n"
    monkeypatch.setattr(subprocess, "check_output", _check_output_table(table))


def test_get_trio_keychain(monkeypatch):
    _patch_all_accounts(monkeypatch, {"username": "bob", "password_hash": "H", "session_secret": "S"})
    assert auth_secrets.get_username() == "bob"
    assert auth_secrets.get_password_hash() == "H"
    assert auth_secrets.get_session_secret() == "S"


def test_get_trio_headless(monkeypatch, tmp_path):
    monkeypatch.setenv("BETTER_AGENT_HEADLESS_AUTH", "1")
    monkeypatch.setenv("BETTER_AGENT_USERNAME", "headless-bob")
    h = tmp_path / "h"
    h.write_text("HH")
    s = tmp_path / "s"
    s.write_text("SS")
    monkeypatch.setenv("BETTER_AGENT_PASSWORD_HASH_FILE", str(h))
    monkeypatch.setenv("BETTER_AGENT_SESSION_SECRET_FILE", str(s))
    assert auth_secrets.get_username() == "headless-bob"
    assert auth_secrets.get_password_hash() == "HH"
    assert auth_secrets.get_session_secret() == "SS"


# --------------------------------------------------------------------------- #
# read_all_parallel
# --------------------------------------------------------------------------- #
def test_read_all_parallel(monkeypatch):
    _patch_all_accounts(monkeypatch, {"username": "p", "password_hash": "PH", "session_secret": "PS"})
    user, ph, ps = auth_secrets.read_all_parallel()
    assert (user, ph, ps) == ("p", "PH", "PS")


# --------------------------------------------------------------------------- #
# _account_exists
# --------------------------------------------------------------------------- #
def _run_returning(returncode):
    def _fake(cmd, *args, **kwargs):
        result = subprocess.CompletedProcess(cmd, returncode)
        return result

    return _fake


def test_account_exists_darwin_found(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _run_returning(0))
    assert auth_secrets._account_exists("username") is True


def test_account_exists_darwin_missing(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _run_returning(44))
    assert auth_secrets._account_exists("username") is False


def test_account_exists_non_darwin_found(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("keyring.get_password", lambda service, account: "x")
    assert auth_secrets._account_exists("username") is True


def test_account_exists_non_darwin_missing(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("keyring.get_password", lambda service, account: None)
    assert auth_secrets._account_exists("username") is False


# --------------------------------------------------------------------------- #
# needs_bootstrap
# --------------------------------------------------------------------------- #
def test_needs_bootstrap_headless_is_false(monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_HEADLESS_AUTH", "1")
    assert auth_secrets.needs_bootstrap() is False


def test_needs_bootstrap_all_present(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _run_returning(0))
    assert auth_secrets.needs_bootstrap() is False


def test_needs_bootstrap_some_missing(monkeypatch):
    def _fake(cmd, *args, **kwargs):
        account = _argv_val(cmd, "-a")
        return subprocess.CompletedProcess(cmd, 0 if account == "username" else 44)

    monkeypatch.setattr(subprocess, "run", _fake)
    assert auth_secrets.needs_bootstrap() is True


# --------------------------------------------------------------------------- #
# _kc_set
# --------------------------------------------------------------------------- #
def test_kc_set_writes_every_service(monkeypatch):
    recorded = []
    monkeypatch.setattr("oskeychain.store", lambda service, account, value: recorded.append((service, account, value)))
    auth_secrets._kc_set("username", "bob")
    services = auth_secrets._services()
    assert {s for s, _, _ in recorded} == set(services)
    assert all(v == "bob" and a == "username" for _, a, v in recorded)


# --------------------------------------------------------------------------- #
# make_password_hash
# --------------------------------------------------------------------------- #
def test_make_password_hash_verifies():
    import argon2

    h = auth_secrets.make_password_hash("hunter2")
    assert h.startswith("$argon2id$")
    argon2.PasswordHasher().verify(h, "hunter2")


# --------------------------------------------------------------------------- #
# _reject_headless_write
# --------------------------------------------------------------------------- #
def test_reject_headless_write_raises_outside_test_mode(monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_HEADLESS_AUTH", "1")
    monkeypatch.setattr("paths.is_test_mode", lambda: False)
    with pytest.raises(RuntimeError, match="cannot be changed at runtime"):
        auth_secrets._reject_headless_write()


def test_reject_headless_write_noop_when_not_headless():
    # test-mode is engaged by conftest; not headless -> no raise
    auth_secrets._reject_headless_write()


def test_reject_headless_write_noop_in_test_mode(monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_HEADLESS_AUTH", "1")
    # paths.is_test_mode() is True in the test home -> carve-out, no raise
    auth_secrets._reject_headless_write()


# --------------------------------------------------------------------------- #
# _headless_write_credentials
# --------------------------------------------------------------------------- #
def test_headless_write_credentials_hash_file_unset(monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_USERNAME", "u")
    # password hash file deliberately unset
    with pytest.raises(RuntimeError, match="BETTER_AGENT_PASSWORD_HASH_FILE"):
        auth_secrets._headless_write_credentials("u", "HH", "SS")


def test_headless_write_credentials_with_secret_file(monkeypatch, tmp_path):
    h = tmp_path / "h"
    h.write_text("old")
    s = tmp_path / "s"
    s.write_text("old")
    monkeypatch.setenv("BETTER_AGENT_PASSWORD_HASH_FILE", str(h))
    monkeypatch.setenv("BETTER_AGENT_SESSION_SECRET_FILE", str(s))

    auth_secrets._headless_write_credentials("ofek", "NEWHASH", "NEWSECRET")

    assert os.environ["BETTER_AGENT_USERNAME"] == "ofek"
    assert h.read_text() == "NEWHASH"
    assert s.read_text() == "NEWSECRET"


def test_headless_write_credentials_ephemeral_secret(monkeypatch, tmp_path):
    h = tmp_path / "h"
    h.write_text("old")
    monkeypatch.setenv("BETTER_AGENT_PASSWORD_HASH_FILE", str(h))
    monkeypatch.delenv("BETTER_AGENT_SESSION_SECRET_FILE", raising=False)

    auth_secrets._headless_write_credentials("ofek", "NEWHASH", "NEWSECRET")

    assert h.read_text() == "NEWHASH"
    assert auth_secrets._headless_ephemeral_session_secret == "NEWSECRET"


# --------------------------------------------------------------------------- #
# _persist_credentials
# --------------------------------------------------------------------------- #
def test_persist_credentials_empty_username():
    with pytest.raises(ValueError, match="non-empty"):
        auth_secrets._persist_credentials("", "pw")


def test_persist_credentials_empty_password():
    with pytest.raises(ValueError, match="non-empty"):
        auth_secrets._persist_credentials("u", "")


def test_persist_credentials_headless(monkeypatch, tmp_path):
    monkeypatch.setenv("BETTER_AGENT_HEADLESS_AUTH", "1")
    h = tmp_path / "h"
    h.write_text("old")
    s = tmp_path / "s"
    s.write_text("old")
    monkeypatch.setenv("BETTER_AGENT_PASSWORD_HASH_FILE", str(h))
    monkeypatch.setenv("BETTER_AGENT_SESSION_SECRET_FILE", str(s))
    monkeypatch.setattr(auth_secrets, "make_password_hash", lambda pw: f"HASH({pw})")

    auth_secrets._persist_credentials("ofek", "pw")

    assert os.environ["BETTER_AGENT_USERNAME"] == "ofek"
    assert h.read_text() == "HASH(pw)"
    assert len(s.read_text()) == 64  # session_secret is token_hex(32)


def test_persist_credentials_keychain(monkeypatch):
    recorded = []
    monkeypatch.setattr(auth_secrets, "make_password_hash", lambda pw: f"HASH({pw})")
    monkeypatch.setattr("oskeychain.store", lambda service, account, value: recorded.append((account, value)))

    auth_secrets._persist_credentials("ofek", "pw")

    written = {acct: val for acct, val in recorded}
    assert written["username"] == "ofek"
    assert written["password_hash"] == "HASH(pw)"
    assert len(written["session_secret"]) == 64


# --------------------------------------------------------------------------- #
# write_credentials / write_login_credentials
# --------------------------------------------------------------------------- #
def test_write_credentials_keychain(monkeypatch):
    recorded = []
    monkeypatch.setattr(auth_secrets, "make_password_hash", lambda pw: f"H({pw})")
    monkeypatch.setattr("oskeychain.store", lambda service, account, value: recorded.append(account))
    auth_secrets.write_credentials("ofek", "pw")
    assert set(recorded) == {"username", "password_hash", "session_secret"}


def test_write_login_credentials_keychain(monkeypatch):
    recorded = []
    monkeypatch.setattr(auth_secrets, "make_password_hash", lambda pw: f"H({pw})")
    monkeypatch.setattr("oskeychain.store", lambda service, account, value: recorded.append(account))
    auth_secrets.write_login_credentials("ofek2", "pw2")
    assert set(recorded) == {"username", "password_hash", "session_secret"}


def test_write_credentials_rejects_headless_outside_test_mode(monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_HEADLESS_AUTH", "1")
    monkeypatch.setattr("paths.is_test_mode", lambda: False)
    with pytest.raises(RuntimeError, match="cannot be changed at runtime"):
        auth_secrets.write_credentials("ofek", "pw")
