"""Hermetic unit tests for oskeychain.

oskeychain reads/writes the OS credential store. The real keychain is
non-hermetic and has no place in the unit tier, so every test here stubs the
boundary surface (subprocess on darwin, keyring + macOS api off darwin, ctypes
for the interaction-allow toggle) and never reaches a real credential. Both
platform branches are forced via sys.platform patching so the suite is
deterministic regardless of host.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import types
from contextlib import contextmanager
from types import SimpleNamespace

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-oskeychain-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import ctypes  # noqa: E402
import oskeychain  # noqa: E402


# --- patching primitives (single source of truth for save/restore) ---


@contextmanager
def _patch(obj, name, value):
    saved = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield value
    finally:
        setattr(obj, name, saved)


@contextmanager
def _platform(name):
    with _patch(sys, "platform", name):
        yield


@contextmanager
def _env(key, value):
    saved = os.environ.get(key)
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


@contextmanager
def _modules(mapping):
    """Install fake modules into sys.modules, restoring prior entries on exit."""
    saved = {name: sys.modules.get(name) for name in mapping}
    for name, module in mapping.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    try:
        yield
    finally:
        for name, prior in saved.items():
            if prior is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prior


# --- fake boundaries ---


def _proc(returncode, stdout=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout)


def _subproc_run(*, returncode=0, stdout="", timeout=False):
    """A subprocess.run stand-in with fixed behavior for darwin paths."""

    def _run(cmd, **kwargs):
        if timeout:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 5))
        return _proc(returncode, stdout)

    return _run


class _FakeKeyringError(Exception):
    pass


class _FakePasswordDeleteError(_FakeKeyringError):
    pass


class _FakeBackend:
    def __init__(self):
        self.calls = []
        self.raise_on = {}
        self.store = {}

    def set_password(self, service, account, value):
        self.calls.append(("set", service, account, value))
        exc = self.raise_on.get("set")
        if exc is not None:
            raise exc
        self.store[(service, account)] = value

    def get_password(self, service, account):
        self.calls.append(("get", service, account))
        exc = self.raise_on.get("get")
        if exc is not None:
            raise exc
        return self.store.get((service, account))

    def delete_password(self, service, account):
        self.calls.append(("del", service, account))
        exc = self.raise_on.get("del")
        if exc is not None:
            raise exc
        self.store.pop((service, account), None)


class _KeyedBackend(_FakeBackend):
    # Class attribute so hasattr(type(backend), "keyring_key") is True, mirroring
    # the real keyring backend whose keyring_key lives on the class.
    keyring_key = None


def _keyring_modules(backend):
    keyring = types.ModuleType("keyring")
    keyring.get_keyring = lambda: backend
    keyring.set_password = backend.set_password
    keyring.get_password = backend.get_password
    keyring.delete_password = backend.delete_password
    errors = types.ModuleType("keyring.errors")
    errors.KeyringError = _FakeKeyringError
    errors.PasswordDeleteError = _FakePasswordDeleteError
    keyring.errors = errors
    return {"keyring": keyring, "keyring.errors": errors}


class _MacOSApi:
    class Error(Exception):
        pass

    class NotFound(Error):
        pass

    def __init__(self):
        self.calls = []
        self.raise_on = {}
        self.find_result = "native-value"

    def find_generic_password(self, keychain, service, account, not_found_ok=False):
        self.calls.append(("find", service, account))
        exc = self.raise_on.get("find")
        if exc is not None:
            raise exc
        return self.find_result

    def set_generic_password(self, keychain, service, account, value):
        self.calls.append(("set", service, account, value))
        exc = self.raise_on.get("set")
        if exc is not None:
            raise exc

    def delete_generic_password(self, keychain, service, account):
        self.calls.append(("del", service, account))
        exc = self.raise_on.get("del")
        if exc is not None:
            raise exc


def _macos_modules(api_obj):
    keyring = types.ModuleType("keyring")
    backends = types.ModuleType("keyring.backends")
    macos = types.ModuleType("keyring.backends.macOS")
    macos.api = api_obj
    keyring.backends = backends
    backends.macOS = macos
    return {
        "keyring": keyring,
        "keyring.backends": backends,
        "keyring.backends.macOS": macos,
    }


class _SecFunc:
    def __init__(self, retval):
        self.retval = retval
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.retval


def _cdll_factory(retval):
    def _factory(_path):
        return SimpleNamespace(SecKeychainSetUserInteractionAllowed=_SecFunc(retval))

    return _factory


# --- _interactive_arg / _store_input ---


def test_interactive_arg_quotes_and_escapes():
    assert oskeychain._interactive_arg("svc") == '"svc"'
    assert oskeychain._interactive_arg('a"b') == '"a\\"b"'
    assert oskeychain._interactive_arg("a\\b") == '"a\\\\b"'
    print("ok  _interactive_arg quotes and escapes")


def test_interactive_arg_rejects_control_characters():
    for bad in ("\0", "\r", "\n"):
        try:
            oskeychain._interactive_arg(f"x{bad}y")
            raise AssertionError(f"accepted control char {bad!r}")
        except ValueError:
            pass
    print("ok  _interactive_arg rejects control characters")


def test_store_input_builds_security_cli_payload():
    payload = oskeychain._store_input("svc", "acct", "secret")
    assert isinstance(payload, bytes)
    text = payload.decode("utf-8")
    assert text.endswith("\n")
    assert '"add-generic-password"' in text
    assert '"-U"' in text
    assert '"-X" "736563726574"' in text  # hex of "secret"
    assert '"-s" "svc"' in text
    assert '"-a" "acct"' in text
    print("ok  _store_input builds the /usr/bin/security payload")


# --- _keyring ---


def test_keyring_returns_backend_and_applies_env_key():
    backend = _KeyedBackend()
    modules = _keyring_modules(backend)
    with _modules(modules), _env("KEYRING_PROPERTY_KEYRING_KEY", "kk"):
        assert oskeychain._keyring() is modules["keyring"]
        assert backend.keyring_key == "kk"
    print("ok  _keyring applies KEYRING_PROPERTY_KEYRING_KEY when the backend supports it")


def test_keyring_skips_env_key_when_backend_lacks_attr():
    backend = _FakeBackend()
    with _modules(_keyring_modules(backend)), _env("KEYRING_PROPERTY_KEYRING_KEY", "kk"):
        oskeychain._keyring()
        assert not hasattr(backend, "keyring_key")
    print("ok  _keyring ignores the env key when the backend has no keyring_key attr")


# --- _set_native_user_interaction / disable / native_user_interaction ---


def test_set_native_user_interaction_non_darwin_is_noop():
    with _platform("linux"):
        assert oskeychain._set_native_user_interaction(True) is None
        assert oskeychain._set_native_user_interaction(False) is None
    print("ok  _set_native_user_interaction is a no-op off darwin")


def test_set_native_user_interaction_darwin_success_and_failure():
    with _platform("darwin"), _patch(ctypes, "CDLL", _cdll_factory(0)):
        assert oskeychain._set_native_user_interaction(True) is None
    with _platform("darwin"), _patch(ctypes, "CDLL", _cdll_factory(1)):
        try:
            oskeychain._set_native_user_interaction(False)
            raise AssertionError("expected RuntimeError on interaction failure")
        except RuntimeError as e:
            assert "interaction" in str(e)
    print("ok  _set_native_user_interaction succeeds at rc=0 and raises at nonzero")


def test_disable_native_user_interaction_delegates():
    calls = []

    def _spy(allowed):
        calls.append(allowed)

    with _patch(oskeychain, "_set_native_user_interaction", _spy):
        oskeychain.disable_native_user_interaction()
    assert calls == [False]
    print("ok  disable_native_user_interaction disables interaction")


def test_native_user_interaction_non_darwin_passthrough():
    calls = []
    with _platform("linux"), _patch(oskeychain, "_set_native_user_interaction", lambda a: calls.append(a)):
        with oskeychain.native_user_interaction():
            calls.append("inside")
    assert calls == ["inside"]
    print("ok  native_user_interaction is a plain passthrough off darwin")


def test_native_user_interaction_darwin_toggles_and_restores():
    calls = []
    with _platform("darwin"), _patch(oskeychain, "_set_native_user_interaction", lambda a: calls.append(a)):
        with oskeychain.native_user_interaction():
            calls.append("inside")
    assert calls == [True, "inside", False]
    print("ok  native_user_interaction enables for the body and disables after")


# --- store ---


def test_store_darwin_success_timeout_and_denied():
    with _platform("darwin"), _patch(subprocess, "run", _subproc_run(returncode=0)):
        assert oskeychain.store("svc", "acct", "v") is None
    with _platform("darwin"), _patch(subprocess, "run", _subproc_run(timeout=True)):
        try:
            oskeychain.store("svc", "acct", "v")
            raise AssertionError("expected timeout RuntimeError")
        except RuntimeError as e:
            assert "timed out" in str(e)
    with _platform("darwin"), _patch(subprocess, "run", _subproc_run(returncode=1)):
        try:
            oskeychain.store("svc", "acct", "v")
            raise AssertionError("expected denied RuntimeError")
        except RuntimeError as e:
            assert "denied" in str(e)
    print("ok  store darwin: success, timeout, denied")


def test_store_non_darwin_via_keyring():
    backend = _FakeBackend()
    with _platform("linux"), _modules(_keyring_modules(backend)):
        oskeychain.store("svc", "acct", "v")
        assert backend.store[("svc", "acct")] == "v"
        backend.raise_on["set"] = _FakeKeyringError("nope")
        try:
            oskeychain.store("svc", "acct", "v")
            raise AssertionError("expected denied RuntimeError")
        except RuntimeError as e:
            assert "denied" in str(e)
    print("ok  store non-darwin: routes through keyring, maps KeyringError")


# --- get ---


def test_get_darwin_success_missing_denied_timeout():
    with _platform("darwin"), _patch(subprocess, "run", _subproc_run(returncode=0, stdout="the-secret\n")):
        assert oskeychain.get("svc", "acct") == "the-secret\n"
    with _platform("darwin"), _patch(subprocess, "run", _subproc_run(returncode=44)):
        assert oskeychain.get("svc", "acct") is None
    with _platform("darwin"), _patch(subprocess, "run", _subproc_run(returncode=1)):
        try:
            oskeychain.get("svc", "acct")
            raise AssertionError("expected denied RuntimeError")
        except RuntimeError as e:
            assert "denied" in str(e)
    with _platform("darwin"), _patch(subprocess, "run", _subproc_run(timeout=True)):
        try:
            oskeychain.get("svc", "acct")
            raise AssertionError("expected timeout RuntimeError")
        except RuntimeError as e:
            assert "timed out" in str(e)
    print("ok  get darwin: success, missing(44), denied, timeout")


def test_get_non_darwin_via_keyring():
    backend = _FakeBackend()
    backend.store[("svc", "acct")] = "the-secret"
    with _platform("linux"), _modules(_keyring_modules(backend)):
        assert oskeychain.get("svc", "acct") == "the-secret"
        assert oskeychain.get("absent", "acct") is None
        backend.raise_on["get"] = _FakeKeyringError("nope")
        try:
            oskeychain.get("svc", "acct")
            raise AssertionError("expected denied RuntimeError")
        except RuntimeError as e:
            assert "denied" in str(e)
    print("ok  get non-darwin: routes through keyring, maps KeyringError")


# --- delete ---


def test_delete_darwin_success_missing_denied_timeout():
    with _platform("darwin"), _patch(subprocess, "run", _subproc_run(returncode=0)):
        assert oskeychain.delete("svc", "acct") is None
    with _platform("darwin"), _patch(subprocess, "run", _subproc_run(returncode=44)):
        assert oskeychain.delete("svc", "acct") is None  # already absent
    with _platform("darwin"), _patch(subprocess, "run", _subproc_run(returncode=1)):
        try:
            oskeychain.delete("svc", "acct")
            raise AssertionError("expected denied RuntimeError")
        except RuntimeError as e:
            assert "denied" in str(e)
    with _platform("darwin"), _patch(subprocess, "run", _subproc_run(timeout=True)):
        try:
            oskeychain.delete("svc", "acct")
            raise AssertionError("expected timeout RuntimeError")
        except RuntimeError as e:
            assert "timed out" in str(e)
    print("ok  delete darwin: success, missing(44), denied, timeout")


def test_delete_non_darwin_via_keyring():
    backend = _FakeBackend()
    backend.store[("svc", "acct")] = "v"
    with _platform("linux"), _modules(_keyring_modules(backend)):
        oskeychain.delete("svc", "acct")
        assert ("svc", "acct") not in backend.store
        backend.raise_on["del"] = _FakePasswordDeleteError("gone")
        assert oskeychain.delete("svc", "acct") is None  # PasswordDeleteError swallowed
        backend.raise_on["del"] = _FakeKeyringError("nope")
        try:
            oskeychain.delete("svc", "acct")
            raise AssertionError("expected denied RuntimeError")
        except RuntimeError as e:
            assert "denied" in str(e)
    print("ok  delete non-darwin: routes through keyring, swallows PasswordDeleteError")


# --- native_get / native_store / native_delete ---


def test_native_get_darwin_and_non_darwin():
    api = _MacOSApi()
    with _platform("darwin"), _modules(_macos_modules(api)):
        assert oskeychain.native_get("svc", "acct") == "native-value"
        assert api.calls == [("find", "svc", "acct")]
        api.raise_on["find"] = _MacOSApi.Error("nope")
        try:
            oskeychain.native_get("svc", "acct")
            raise AssertionError("expected denied RuntimeError")
        except RuntimeError as e:
            assert "denied" in str(e)
    backend = _FakeBackend()
    backend.store[("svc", "acct")] = "kr"
    with _platform("linux"), _modules(_keyring_modules(backend)):
        assert oskeychain.native_get("svc", "acct") == "kr"
    print("ok  native_get darwin (api + error) and non-darwin (delegates to get)")


def test_native_store_darwin_and_non_darwin():
    api = _MacOSApi()
    with _platform("darwin"), _modules(_macos_modules(api)):
        assert oskeychain.native_store("svc", "acct", "v") is None
        assert api.calls == [("set", "svc", "acct", "v")]
        api.raise_on["set"] = _MacOSApi.Error("nope")
        try:
            oskeychain.native_store("svc", "acct", "v")
            raise AssertionError("expected denied RuntimeError")
        except RuntimeError as e:
            assert "denied" in str(e)
    backend = _FakeBackend()
    with _platform("linux"), _modules(_keyring_modules(backend)):
        oskeychain.native_store("svc", "acct", "v")
        assert backend.store[("svc", "acct")] == "v"
    print("ok  native_store darwin (api + error) and non-darwin (delegates to store)")


def test_native_delete_darwin_notfound_error_and_non_darwin():
    api = _MacOSApi()
    with _platform("darwin"), _modules(_macos_modules(api)):
        assert oskeychain.native_delete("svc", "acct") is None
        assert api.calls == [("del", "svc", "acct")]
        api.raise_on["del"] = _MacOSApi.NotFound("gone")
        assert oskeychain.native_delete("svc", "acct") is None  # NotFound swallowed
        api.raise_on["del"] = _MacOSApi.Error("nope")
        try:
            oskeychain.native_delete("svc", "acct")
            raise AssertionError("expected denied RuntimeError")
        except RuntimeError as e:
            assert "denied" in str(e)
    backend = _FakeBackend()
    backend.store[("svc", "acct")] = "v"
    with _platform("linux"), _modules(_keyring_modules(backend)):
        oskeychain.native_delete("svc", "acct")
        assert ("svc", "acct") not in backend.store
    print("ok  native_delete darwin (api + NotFound + error) and non-darwin (delegates)")


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
    print("\nall oskeychain unit tests passed")
