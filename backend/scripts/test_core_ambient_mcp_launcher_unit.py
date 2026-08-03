"""Unit coverage for core_ambient_mcp_launcher.py.

Distinct from test_ambient_launcher_auth.py, which is a standalone script
testing extension_store's ambient *resolution*/grant path (a different
module). Here we cover this launcher directly: backend-url resolution,
token minting, argv validation, and the sanitized os.execvpe subprocess
spawn — the highest-risk surface, exercised with os.execvpe mocked so the
process is not actually replaced.
"""
from __future__ import annotations

import os
import sys

import core_ambient_mcp_launcher as launcher


# ----------------------------------------------------------------- _backend_url

def _clear_url_env(monkeypatch):
    for name in (
        "BETTER_AGENT_BACKEND_URL", "BETTER_CLAUDE_BACKEND_URL",
        "BETTER_AGENT_BACKEND_PORT", "BETTER_CLAUDE_BACKEND_PORT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_backend_url_explicit(monkeypatch):
    _clear_url_env(monkeypatch)
    monkeypatch.setenv("BETTER_AGENT_BACKEND_URL", "http://host:9000")
    assert launcher._backend_url() == "http://host:9000"


def test_backend_url_explicit_legacy_var(monkeypatch):
    _clear_url_env(monkeypatch)
    monkeypatch.setenv("BETTER_CLAUDE_BACKEND_URL", "http://legacy:1234")
    assert launcher._backend_url() == "http://legacy:1234"


def test_backend_url_strips_whitespace(monkeypatch):
    _clear_url_env(monkeypatch)
    monkeypatch.setenv("BETTER_AGENT_BACKEND_URL", "  http://trimmed:1  ")
    assert launcher._backend_url() == "http://trimmed:1"


def test_backend_url_defaults_to_localhost_with_port(monkeypatch):
    _clear_url_env(monkeypatch)
    monkeypatch.setenv("BETTER_AGENT_BACKEND_PORT", "7000")
    assert launcher._backend_url() == "http://localhost:7000"


def test_backend_url_default_port_when_unset(monkeypatch):
    _clear_url_env(monkeypatch)
    assert launcher._backend_url() == f"http://localhost:{launcher._DEFAULT_BACKEND_PORT}"


# ------------------------------------------------------------- _internal_token

def test_internal_token_mints_for_coordination(monkeypatch):
    import extension_store
    import extension_token_registry

    calls = {}

    def fake_mint(extension_id):
        calls["extension_id"] = extension_id
        return "ambient-tok"

    monkeypatch.setattr(extension_token_registry, "mint", fake_mint)
    assert launcher._internal_token() == "ambient-tok"
    assert calls["extension_id"] == extension_store.BUILTIN_COORDINATION_EXTENSION_ID


# --------------------------------------------------------------------- main()

def test_main_wrong_arg_count_returns_2(capsys, monkeypatch):
    for argv in ([], ["a", "b"], ["a", "b", "c"]):
        rc = launcher.main(argv)
        assert rc == 2
    err = capsys.readouterr().err
    assert "usage" in err


def test_main_unknown_server_returns_1(capsys):
    rc = launcher.main(["nope"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not ambient-eligible" in err
    assert "nope" in err


def _patch_for_main(monkeypatch, *, backend_url, token):
    """Freeze _backend_url/_internal_token so main()'s env wiring is isolated."""
    monkeypatch.setattr(launcher, "_backend_url", lambda: backend_url)
    monkeypatch.setattr(launcher, "_internal_token", lambda: token)
    monkeypatch.setenv("BC_TEST_SECRET_CANARY", "must-not-leak")  # sanitization canary


def test_main_capabilities_spawn_shape_and_sanitized_env(monkeypatch):
    _patch_for_main(monkeypatch, backend_url="http://b:5", token="tok-1")
    captured = {}

    def fake_execvpe(exe, args, env):
        captured["exe"] = exe
        captured["args"] = args
        captured["env"] = env
        # Real execvpe replaces the process and never returns; as a no-op we
        # fall through to main's `return 1`.

    monkeypatch.setattr(os, "execvpe", fake_execvpe)

    rc = launcher.main(["capabilities"])

    assert rc == 1  # post-exec fallback, reached only because execvpe is mocked
    assert captured["exe"] == sys.executable
    assert captured["args"][0] == sys.executable
    assert captured["args"][1].endswith("capabilities_mcp.py")
    assert os.path.dirname(captured["args"][1]) == os.path.dirname(os.path.abspath(launcher.__file__))

    env = captured["env"]
    # sdk path is the repo root (parent of backend/) + "sdk".
    expected_sdk = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(launcher.__file__))), "sdk"
    )
    assert env["PYTHONPATH"] == expected_sdk
    assert env["PYTHONIOENCODING"] == "utf-8"
    # dual_env_many emits both BETTER_AGENT_ and BETTER_CLAUDE_ prefixes.
    assert env["BETTER_AGENT_BACKEND_URL"] == "http://b:5"
    assert env["BETTER_CLAUDE_BACKEND_URL"] == "http://b:5"
    assert env["BETTER_AGENT_INTERNAL_TOKEN"] == "tok-1"
    assert env["BETTER_CLAUDE_INTERNAL_TOKEN"] == "tok-1"
    assert env["BETTER_AGENT_AMBIENT_LAUNCH"] == "1"
    assert env["BETTER_CLAUDE_AMBIENT_LAUNCH"] == "1"
    # Sanitization: env is a fresh dict, not os.environ — the canary must not leak.
    assert "BC_TEST_SECRET_CANARY" not in env
    assert set(env.keys()) == {
        "PATH", "PYTHONIOENCODING", "PYTHONPATH",
        "BETTER_AGENT_BACKEND_URL", "BETTER_CLAUDE_BACKEND_URL",
        "BETTER_AGENT_INTERNAL_TOKEN", "BETTER_CLAUDE_INTERNAL_TOKEN",
        "BETTER_AGENT_AMBIENT_LAUNCH", "BETTER_CLAUDE_AMBIENT_LAUNCH",
    }


def test_main_ui_server_targets_open_file_panel_script(monkeypatch):
    _patch_for_main(monkeypatch, backend_url="http://b:5", token="tok-2")

    captured = {}
    monkeypatch.setattr(os, "execvpe", lambda exe, args, env: captured.update(args=args))
    launcher.main(["ui"])
    assert captured["args"][1].endswith("open_file_panel_mcp.py")
