from __future__ import annotations

import os
import json
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-password-manager-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import oskeychain  # noqa: E402
import password_manager  # noqa: E402


def test_store_service_password_routes_to_os_keychain():
    calls = []
    values = {}
    real_get = oskeychain.get
    real_store = oskeychain.store
    oskeychain.get = lambda service, account: values.get((service, account))
    def _store(service, account, value):
        values[(service, account)] = value
        calls.append((service, account, value))
    oskeychain.store = _store
    try:
        result = password_manager.store_service_password({
            "service": " testape ",
            "account": " login.password ",
            "password": "secret-value",
        })
    finally:
        oskeychain.get = real_get
        oskeychain.store = real_store
    assert result == {"service": "testape", "account": "login.password"}
    assert calls == [
        ("testape", "login.password", "secret-value"),
        (
            password_manager.INDEX_SERVICE,
            password_manager.INDEX_ACCOUNT,
            '[{"service":"testape","account":"login.password"}]',
        ),
        (
            password_manager.LEGACY_INDEX_SERVICE,
            password_manager.INDEX_ACCOUNT,
            '[{"service":"testape","account":"login.password"}]',
        ),
    ]
    print("ok  stores via OS keychain wrapper")


def test_list_and_delete_service_passwords_use_keychain_index():
    values = {
        (password_manager.LEGACY_INDEX_SERVICE, password_manager.INDEX_ACCOUNT): json.dumps([
            {"service": "svc-b", "account": "acct-2"},
        ]),
        (password_manager.INDEX_SERVICE, password_manager.INDEX_ACCOUNT): json.dumps([
            {"service": "svc-a", "account": "acct-1"},
        ]),
    }
    deleted = []
    real_get = oskeychain.get
    real_store = oskeychain.store
    real_delete = oskeychain.delete
    oskeychain.get = lambda service, account: values.get((service, account))
    oskeychain.store = lambda service, account, value: values.__setitem__((service, account), value)
    oskeychain.delete = lambda service, account: deleted.append((service, account))
    try:
        listed = password_manager.list_service_passwords()
        deleted_result = password_manager.delete_service_password({
            "service": "svc-b",
            "account": "acct-2",
        })
        relisted = password_manager.list_service_passwords()
    finally:
        oskeychain.get = real_get
        oskeychain.store = real_store
        oskeychain.delete = real_delete
    assert listed == {
        "items": [
            {"service": "svc-a", "account": "acct-1"},
            {"service": "svc-b", "account": "acct-2"},
        ]
    }
    assert deleted_result == {"service": "svc-b", "account": "acct-2"}
    assert deleted == [("svc-b", "acct-2")]
    assert relisted == {"items": [{"service": "svc-a", "account": "acct-1"}]}
    encoded = '[{"service":"svc-a","account":"acct-1"}]'
    assert values[(password_manager.INDEX_SERVICE, password_manager.INDEX_ACCOUNT)] == encoded
    assert values[(password_manager.LEGACY_INDEX_SERVICE, password_manager.INDEX_ACCOUNT)] == encoded
    print("ok  lists and deletes via OS keychain index")


def test_get_service_password_requires_keychain_index():
    values = {
        (password_manager.INDEX_SERVICE, password_manager.INDEX_ACCOUNT): json.dumps([
            {"service": "ofekdev", "account": "sftp.pass"},
        ]),
        ("ofekdev", "sftp.pass"): "deploy-pass",
        ("other", "secret"): "must-not-read",
    }
    real_get = oskeychain.get
    oskeychain.get = lambda service, account: values.get((service, account))
    try:
        assert password_manager.has_service_password("ofekdev", "sftp.pass") is True
        assert password_manager.get_service_password("ofekdev", "sftp.pass") == "deploy-pass"
        assert password_manager.has_service_password("other", "secret") is False
        try:
            password_manager.get_service_password("other", "secret")
            raise AssertionError("unindexed password was returned")
        except password_manager.PasswordManagerError as e:
            assert str(e) == "password not found"
    finally:
        oskeychain.get = real_get
    print("ok  reads only indexed password-manager entries")


def test_store_service_password_rejects_unsafe_shape():
    cases = [
        ({}, "service must be a string"),
        ({"service": "svc", "account": "acct"}, "password must be a string"),
        ({"service": "svc/name", "account": "acct", "password": "x"}, "service cannot contain /"),
        ({"service": "svc", "account": "bad/name", "password": "x"}, "account cannot contain /"),
        ({"service": "svc\nx", "account": "acct", "password": "x"}, "service contains control characters"),
        ({"service": "svc", "account": "acct", "password": ""}, "password is required"),
        ({"service": "svc", "account": "acct", "password": "x", "extra": "no"}, "unexpected field"),
        ({"service": "better-agent", "account": "session_secret", "password": "x"}, "service is reserved"),
        ({"service": "better-claude", "account": "session_secret", "password": "x"}, "service is reserved"),
        ({"service": "Claude Code-credentials", "account": "oauth", "password": "x"}, "service is reserved"),
    ]
    for payload, expected in cases:
        try:
            password_manager.store_service_password(payload)
            raise AssertionError(f"accepted invalid payload: {payload!r}")
        except password_manager.PasswordManagerError as e:
            assert str(e) == expected
    print("ok  rejects invalid password-manager payloads")


def _run_all():
    tests = [
        test_store_service_password_routes_to_os_keychain,
        test_list_and_delete_service_passwords_use_keychain_index,
        test_get_service_password_requires_keychain_index,
        test_store_service_password_rejects_unsafe_shape,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {test.__name__}: {e}")
            import traceback

            traceback.print_exc()
    return failed


if __name__ == "__main__":
    try:
        rc = _run_all()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if rc:
        print(f"\n{rc} test(s) failed")
        sys.exit(1)
    print("\nall password-manager tests passed")
