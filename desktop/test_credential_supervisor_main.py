from __future__ import annotations

import os
import sys
import types

import credential_supervisor_main as mod


def test_self_test_sets_self_test_env_during_keyring_then_restores(monkeypatch):
    import headless_keyring

    instances = []

    def make_spy(*a, **kw):
        inst = types.SimpleNamespace(env_at_init=dict(os.environ))
        instances.append(inst)
        return inst

    monkeypatch.setattr(headless_keyring, "Keyring", make_spy)

    # Mixed prior state so both restore branches run: two set, two unset.
    monkeypatch.setenv("HOME", "/tmp/prior-home")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/prior-cfg")
    monkeypatch.delenv("BETTER_AGENT_HEADLESS_KEYRING_KEY", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    rc = mod._self_test()

    assert rc == 0
    # During Keyring() the four keys held the self-test values (root for the
    # three path keys, the marker for the keyring key).
    env = instances[0].env_at_init
    root = env["HOME"]
    assert env["HOME"] == root
    assert env["XDG_CONFIG_HOME"] == root
    assert env["XDG_DATA_HOME"] == root
    assert root  # non-empty tempdir path
    assert env["BETTER_AGENT_HEADLESS_KEYRING_KEY"] == "self-test-only"
    # After return every key is back to its prior state.
    assert os.environ["HOME"] == "/tmp/prior-home"
    assert os.environ["XDG_CONFIG_HOME"] == "/tmp/prior-cfg"
    assert "BETTER_AGENT_HEADLESS_KEYRING_KEY" not in os.environ
    assert "XDG_DATA_HOME" not in os.environ


def test_main_self_test_flag_runs_self_test(monkeypatch):
    import headless_keyring

    calls = {"n": 0}

    def make_spy(*a, **kw):
        calls["n"] += 1
        return object()

    monkeypatch.setattr(headless_keyring, "Keyring", make_spy)
    monkeypatch.delenv("BETTER_AGENT_HEADLESS_KEYRING_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["prog", "--self-test"])

    assert mod.main() == 0
    assert calls["n"] == 1


def test_main_delegates_to_browser_backend_supervisor(monkeypatch):
    invoked = {"called": False}

    def fake_supervisor_main():
        invoked["called"] = True
        return 7

    monkeypatch.setitem(
        sys.modules,
        "browser_backend_supervisor",
        types.SimpleNamespace(main=fake_supervisor_main),
    )
    monkeypatch.setattr(sys, "argv", ["prog"])

    assert mod.main() == 7
    assert invoked["called"] is True


def test_main_extra_args_still_delegate_to_supervisor(monkeypatch):
    # An argv that is not exactly ["--self-test"] must not take the self-test
    # branch, even if it begins with --self-test.
    invoked = {"called": False}

    def fake_supervisor_main():
        invoked["called"] = True
        return 9

    monkeypatch.setitem(
        sys.modules,
        "browser_backend_supervisor",
        types.SimpleNamespace(main=fake_supervisor_main),
    )
    monkeypatch.setattr(sys, "argv", ["prog", "--self-test", "extra"])

    assert mod.main() == 9
    assert invoked["called"] is True
