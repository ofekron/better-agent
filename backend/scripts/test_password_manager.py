from __future__ import annotations

import os
import json
import shutil
import sys
from contextlib import contextmanager

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-password-manager-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import oskeychain  # noqa: E402
import password_manager  # noqa: E402


@contextmanager
def _keychain_stub(values=None):
    """In-memory stand-in for the OS keychain.

    Swaps oskeychain.get/store/delete for dict-backed fakes that never touch
    real credentials, and restores them on exit. Yields ``(values, stores,
    deletes)`` where ``stores``/``deletes`` are ordered call logs. Seeding
    ``values`` with ``(service, account) -> json`` entries feeds the
    password-manager index just like a real keychain would.
    """
    values = {} if values is None else dict(values)
    stores: list[tuple] = []
    deletes: list[tuple] = []
    real_get, real_store, real_delete = oskeychain.get, oskeychain.store, oskeychain.delete
    oskeychain.get = lambda service, account: values.get((service, account))

    def _store(service, account, value):
        values[(service, account)] = value
        stores.append((service, account, value))

    def _delete(service, account):
        deletes.append((service, account))

    oskeychain.store = _store
    oskeychain.delete = _delete
    try:
        yield values, stores, deletes
    finally:
        oskeychain.get, oskeychain.store, oskeychain.delete = real_get, real_store, real_delete


def test_store_service_password_routes_to_os_keychain():
    with _keychain_stub() as (_values, stores, _deletes):
        result = password_manager.store_service_password({
            "service": " testape ",
            "account": " login.password ",
            "password": "secret-value",
        })
    assert result == {"service": "testape", "account": "login.password"}
    assert stores == [
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
    with _keychain_stub(values) as (values, _stores, deletes):
        listed = password_manager.list_service_passwords()
        deleted_result = password_manager.delete_service_password({
            "service": "svc-b",
            "account": "acct-2",
        })
        relisted = password_manager.list_service_passwords()
    assert listed == {
        "items": [
            {"service": "svc-a", "account": "acct-1"},
            {"service": "svc-b", "account": "acct-2"},
        ]
    }
    assert deleted_result == {"service": "svc-b", "account": "acct-2"}
    assert deletes == [("svc-b", "acct-2")]
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
    with _keychain_stub(values):
        assert password_manager.has_service_password("ofekdev", "sftp.pass") is True
        assert password_manager.get_service_password("ofekdev", "sftp.pass") == "deploy-pass"
        assert password_manager.has_service_password("other", "secret") is False
        try:
            password_manager.get_service_password("other", "secret")
            raise AssertionError("unindexed password was returned")
        except password_manager.PasswordManagerError as e:
            assert str(e) == "password not found"
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
    with _keychain_stub():
        for payload, expected in cases:
            try:
                password_manager.store_service_password(payload)
                raise AssertionError(f"accepted invalid payload: {payload!r}")
            except password_manager.PasswordManagerError as e:
                assert str(e) == expected
    print("ok  rejects invalid password-manager payloads")


def test_clean_text_rejects_blank_and_oversize_fields():
    overlong_service = "x" * (password_manager.MAX_SERVICE_LEN + 1)
    overlong_account = "x" * (password_manager.MAX_ACCOUNT_LEN + 1)
    overlong_password = "x" * (password_manager.MAX_PASSWORD_LEN + 1)
    cases = [
        ({"service": "   ", "account": "acct", "password": "x"}, "service is required"),
        ({"service": "svc", "account": "\t", "password": "x"}, "account is required"),
        ({"service": overlong_service, "account": "acct", "password": "x"}, "service is too long"),
        ({"service": "svc", "account": overlong_account, "password": "x"}, "account is too long"),
        ({"service": "svc", "account": "acct", "password": overlong_password}, "password is too long"),
    ]
    with _keychain_stub():
        for payload, expected in cases:
            try:
                password_manager.store_service_password(payload)
                raise AssertionError(f"accepted oversize/blank payload: {payload!r}")
            except password_manager.PasswordManagerError as e:
                assert str(e) == expected
    print("ok  rejects blank and over-length fields")


def test_store_and_delete_reject_non_dict_body():
    with _keychain_stub():
        for bad in ("not-a-dict", 123, None, ["svc"]):
            try:
                password_manager.store_service_password(bad)
                raise AssertionError(f"store accepted non-dict body: {bad!r}")
            except password_manager.PasswordManagerError as e:
                assert str(e) == "body must be an object"
            try:
                password_manager.delete_service_password(bad)
                raise AssertionError(f"delete accepted non-dict body: {bad!r}")
            except password_manager.PasswordManagerError as e:
                assert str(e) == "body must be an object"
    print("ok  rejects non-object bodies for store and delete")


def test_delete_service_password_rejects_unexpected_field_and_reserved_service():
    with _keychain_stub():
        try:
            password_manager.delete_service_password({"service": "svc", "account": "a", "extra": "x"})
            raise AssertionError("delete accepted unexpected field")
        except password_manager.PasswordManagerError as e:
            assert str(e) == "unexpected field"
        try:
            password_manager.delete_service_password({"service": "better-agent", "account": "a"})
            raise AssertionError("delete accepted reserved service")
        except password_manager.PasswordManagerError as e:
            assert str(e) == "service is reserved"
    print("ok  rejects unexpected fields and reserved services on delete")


def test_get_service_password_raises_when_value_missing_from_keychain():
    # Indexed but the underlying keychain entry is absent: get must fail closed.
    values = {
        (password_manager.INDEX_SERVICE, password_manager.INDEX_ACCOUNT): json.dumps([
            {"service": "ghost", "account": "acct"},
        ]),
    }
    with _keychain_stub(values):
        assert password_manager.has_service_password("ghost", "acct") is False
        try:
            password_manager.get_service_password("ghost", "acct")
            raise AssertionError("returned a password the keychain does not hold")
        except password_manager.PasswordManagerError as e:
            assert str(e) == "password not found"
    print("ok  fails closed when an indexed entry has no keychain value")


def test_read_index_rejects_corrupt_payload():
    corrupt = [
        ("not-json{", "invalid JSON"),
        (json.dumps({"not": "a-list"}), "non-list index"),
        (json.dumps([123]), "non-dict index item"),
    ]
    for raw, label in corrupt:
        values = {(password_manager.INDEX_SERVICE, password_manager.INDEX_ACCOUNT): raw}
        with _keychain_stub(values):
            try:
                password_manager.list_service_passwords()
                raise AssertionError(f"accepted {label}")
            except password_manager.PasswordManagerError as e:
                assert str(e) == "password manager index is invalid"
    print("ok  rejects a corrupt password-manager index")


def test_add_index_item_is_idempotent_for_existing_entry():
    encoded = json.dumps([{"service": "svc", "account": "acct"}])
    values = {(password_manager.INDEX_SERVICE, password_manager.INDEX_ACCOUNT): encoded}
    with _keychain_stub(values) as (_values, stores, _deletes):
        password_manager.store_service_password({"service": "svc", "account": "acct", "password": "p"})
        items = password_manager.list_service_passwords()["items"]
    # The entry already exists, so the index is rewritten with a single item, no duplicate.
    assert items == [{"service": "svc", "account": "acct"}]
    index_writes = [
        call for call in stores
        if call[1] == password_manager.INDEX_ACCOUNT
    ]
    assert len(index_writes) == 2  # primary + legacy, still one logical entry
    print("ok  does not duplicate an existing index entry")


def _run_all():
    tests = [
        obj for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
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
