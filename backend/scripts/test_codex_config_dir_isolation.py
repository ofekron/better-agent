"""Locks per-account credential-dir isolation for env-selectable providers.

Before the fix, a codex provider record's `config_dir` was ignored: every
codex run symlinked the single real `~/.codex`, so two codex accounts shared
one login. Claude already isolated via `CLAUDE_CONFIG_DIR`. This test locks:

- `provider_credential_env` is the single source of truth mapping a record to
  its `(env_var, dir)` — CODEX_HOME for codex/fugu, CLAUDE_CONFIG_DIR for
  claude — and returns None for the shared default dir / no config_dir /
  kinds with no env-selectable store.
- `CodexProvider.build_env` sets CODEX_HOME per record (default → leaves any
  ambient CODEX_HOME untouched), so two records isolate.
- `ClaudeProvider.build_env` still isolates via CLAUDE_CONFIG_DIR (regression).
- `engine.env` exports the active account's credential env var for manual login.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="codex_cfgdir_home_")
paths.engage_test_home(_TMP)

import config_store  # noqa: E402
from provider_codex import CodexProvider  # noqa: E402
from provider_claude import ClaudeProvider  # noqa: E402

HOME = paths.user_home()
WORK = str(Path(_TMP) / "codex-work")
PERSONAL = str(Path(_TMP) / "codex-personal")


def _rec(kind, config_dir, mode="subscription"):
    return {"id": f"{kind}-{config_dir}", "kind": kind, "mode": mode,
            "config_dir": config_dir, "base_url": ""}


def test_credential_env_ssot():
    # codex isolated dir → CODEX_HOME
    assert config_store.provider_credential_env(_rec("codex", WORK)) == (
        "CODEX_HOME", str(paths.resolve_provider_config_dir(WORK)))
    # codex shared default ($HOME/.codex, ~/.codex, empty) → None (no override)
    for default in ("$HOME/.codex", "~/.codex", str(HOME / ".codex"), ""):
        assert config_store.provider_credential_env(_rec("codex", default)) is None, default
    # fugu reuses the codex runner → also CODEX_HOME
    assert config_store.provider_credential_env(_rec("fugu", WORK))[0] == "CODEX_HOME"
    # claude isolated → CLAUDE_CONFIG_DIR; default → None
    assert config_store.provider_credential_env(
        _rec("claude", str(Path(_TMP) / "claude-b")))[0] == "CLAUDE_CONFIG_DIR"
    assert config_store.provider_credential_env(_rec("claude", "$HOME/.claude")) is None
    # kinds with no env-selectable store → None even with a config_dir
    assert config_store.provider_credential_env(_rec("gemini", WORK)) is None
    # two distinct codex accounts isolate
    a = config_store.provider_credential_env(_rec("codex", WORK))
    b = config_store.provider_credential_env(_rec("codex", PERSONAL))
    assert a[1] != b[1]


def test_codex_build_env_isolates():
    # ambient CODEX_HOME preserved for a default-dir record...
    os.environ["CODEX_HOME"] = "/ambient/codex"
    try:
        env = CodexProvider(_rec("codex", "$HOME/.codex")).build_env()
        assert env["CODEX_HOME"] == "/ambient/codex"
        # ...but an isolated record overrides it, and claude vars are cleared.
        env = CodexProvider(_rec("codex", WORK)).build_env()
        assert env["CODEX_HOME"] == str(paths.resolve_provider_config_dir(WORK))
        assert "CLAUDE_CONFIG_DIR" not in env
    finally:
        os.environ.pop("CODEX_HOME", None)


def test_claude_build_env_isolates():
    os.environ["CLAUDE_CONFIG_DIR"] = "/ambient/claude"
    try:
        # default record clears the ambient value
        env = ClaudeProvider(_rec("claude", "$HOME/.claude")).build_env()
        assert "CLAUDE_CONFIG_DIR" not in env
        # isolated record sets it
        env = ClaudeProvider(_rec("claude", str(Path(_TMP) / "claude-b"))).build_env()
        assert env["CLAUDE_CONFIG_DIR"] == str(
            paths.resolve_provider_config_dir(str(Path(_TMP) / "claude-b")))
    finally:
        os.environ.pop("CLAUDE_CONFIG_DIR", None)


def test_engine_env_exports_active_credential_dir():
    pid = config_store.add_provider(
        {"name": "codex work", "kind": "codex", "mode": "subscription",
         "config_dir": WORK})["id"]
    config_store.set_default_provider(pid)
    config_store.apply_env_vars(pid)
    text = config_store._engine_env_path().read_text()
    assert f"export CODEX_HOME='{paths.resolve_provider_config_dir(WORK)}'" in text
    assert "unset CLAUDE_CONFIG_DIR" in text


def _run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("ALL PASS")


if __name__ == "__main__":
    _run()
