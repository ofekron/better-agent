"""Dedicated pytest owner for operation_cli.

The prior owner (scripts/test_operation_cli.py) is a standalone __main__
script, so pytest collects 0 items and operation_cli was effectively
ownerless at the pytest tier. This owner drives every callable and branch
hermetically: launcher file-IO (atomic temp+replace write and the
idempotent content-unchanged early-return), core-config construction for
both frozen and non-frozen layouts, the env-driven available_configs
projection with extension-group filtering, the CLI dispatch in main()
(list / no-args / invalid-group / unavailable / happy), and the execvpe
command-build path in _exec_config with os.execvpe patched so the test
process is never replaced.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths  # noqa: E402

paths.engage_test_home(tempfile.mkdtemp(prefix="operation-cli-unit-"))

import json  # noqa: E402

import pytest  # noqa: E402

import extension_store  # noqa: E402
import operation_cli  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path):
    paths.engage_test_home(str(tmp_path))
    paths.reset_home_cache()
    yield


# --- _core_config -----------------------------------------------------------


def test_core_config_non_frozen_resolves_script_paths():
    for group, script in operation_cli._CORE_SCRIPTS.items():
        cfg = operation_cli._core_config(script)
        assert cfg["command"] == sys.executable
        assert cfg["args"] == [str(Path(operation_cli.__file__).with_name(script))]
        assert cfg["env"] == {}


def test_core_config_frozen_uses_flag_args(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cfg = operation_cli._core_config("communicate_mcp.py")
    assert cfg["command"] == sys.executable
    assert cfg["args"] == ["--communicate-mcp"]
    assert cfg["env"] == {}


# --- _write_launcher / install_launcher -------------------------------------


def test_write_launcher_creates_executable_file(tmp_path):
    target = tmp_path / "out"
    operation_cli._write_launcher(target, "payload", executable=True)
    assert target.read_text() == "payload"
    assert os.access(target, os.X_OK)


def test_write_launcher_creates_non_executable_file(tmp_path):
    target = tmp_path / "out.cmd"
    operation_cli._write_launcher(target, "data", executable=False)
    assert target.read_text() == "data"
    assert not os.access(target, os.X_OK)


def test_write_launcher_skips_when_content_unchanged(tmp_path):
    target = tmp_path / "out"
    operation_cli._write_launcher(target, "same", executable=False)
    operation_cli._write_launcher(target, "same", executable=False)
    assert target.read_text() == "same"
    # a redundant rewrite must not leave a temp file behind
    assert not list(tmp_path.glob("*.tmp"))


def test_write_launcher_rewrites_when_content_changed(tmp_path):
    target = tmp_path / "out"
    operation_cli._write_launcher(target, "old", executable=False)
    operation_cli._write_launcher(target, "new", executable=False)
    assert target.read_text() == "new"


def test_install_launcher_non_frozen_writes_both_platform_launchers():
    directory = operation_cli.install_launcher()
    posix = directory / "better-agent-cli"
    windows = directory / "better-agent-cli.cmd"
    assert posix.is_file() and os.access(posix, os.X_OK)
    assert windows.is_file()
    posix_text = posix.read_text()
    assert sys.executable in posix_text
    assert str(Path(operation_cli.__file__).resolve()) in posix_text
    assert windows.read_text().startswith("@echo off")


def test_install_launcher_frozen_uses_operation_cli_flag(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    directory = operation_cli.install_launcher()
    posix_text = (directory / "better-agent-cli").read_text()
    assert sys.executable in posix_text
    assert "--operation-cli" in posix_text


# --- _exec_config -----------------------------------------------------------


def test_exec_config_rejects_blank_command():
    with pytest.raises(SystemExit):
        operation_cli._exec_config({"command": "   ", "args": [], "env": {}}, ["op"])


def test_exec_config_builds_argv_merges_env_then_asserts(monkeypatch):
    captured = {}

    def fake_execvpe(command, argv, env):
        captured["command"] = command
        captured["argv"] = argv
        captured["env"] = env

    monkeypatch.setattr(os, "execvpe", fake_execvpe)
    # execvpe is patched to return (it never returns for real); the defensive
    # AssertionError right after it must fire.
    with pytest.raises(AssertionError):
        operation_cli._exec_config(
            {"command": "py", "args": ["--flag", 1], "env": {"FOO": "bar"}},
            ["op", "input"],
        )
    assert captured["command"] == "py"
    assert captured["argv"] == ["py", "--flag", "1", "cli", "op", "input"]
    assert captured["env"]["FOO"] == "bar"
    assert "PATH" in captured["env"]


# --- available_configs ------------------------------------------------------


def _patch_runtime_mcp(monkeypatch, return_value):
    captured = {}

    def fake(inputs, user_facing, bare):
        captured["inputs"] = inputs
        captured["user_facing"] = user_facing
        captured["bare"] = bare
        return return_value

    monkeypatch.setattr(extension_store, "runtime_mcp_server_configs", fake)
    return captured


def test_available_configs_parses_env_and_filters_groups(monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_APP_SESSION_ID", "sess")
    monkeypatch.setenv("BETTER_AGENT_BACKEND_URL", "http://x")
    monkeypatch.setenv("BETTER_AGENT_INTERNAL_TOKEN", "tok")
    monkeypatch.setenv("BETTER_AGENT_CWD", "/proj")
    monkeypatch.setenv("BETTER_AGENT_MODEL", "m")
    monkeypatch.setenv("BETTER_AGENT_PROVIDER_ID", "prov")
    monkeypatch.setenv("BETTER_AGENT_BARE_CONFIG", "1")
    monkeypatch.setenv("BETTER_AGENT_USER_FACING", "1")
    monkeypatch.setenv("BETTER_AGENT_ACTIVE_CAPABILITY_IDS", "a,b")
    monkeypatch.setenv("BETTER_AGENT_DISABLED_BUILTIN_EXTENSIONS", "x,y")

    captured = _patch_runtime_mcp(
        monkeypatch,
        {
            "better-agent-coordination": {"command": "coord"},
            "better-agent-marketplace": {"command": "market"},
            "unmaintained-group": {"command": "other"},
        },
    )

    configs = operation_cli.available_configs()

    assert set(operation_cli._CORE_SCRIPTS) <= set(configs)
    assert "better-agent-coordination" in configs
    assert "better-agent-marketplace" in configs
    assert "unmaintained-group" not in configs
    inputs = captured["inputs"]
    assert inputs["app_session_id"] == "sess"
    assert inputs["backend_url"] == "http://x"
    assert inputs["internal_token"] == "tok"
    assert inputs["cwd"] == "/proj"
    assert inputs["model"] == "m"
    assert inputs["provider_id"] == "prov"
    assert inputs["bare_config"] is True
    assert inputs["user_facing"] is True
    assert inputs["active_capability_ids"] == ["a", "b"]
    assert inputs["disabled_builtin_extensions"] == ["x", "y"]
    assert captured["user_facing"] is True
    assert captured["bare"] is True


def test_available_configs_defaults_when_env_unset(monkeypatch):
    for name in list(os.environ):
        if name.startswith("BETTER_AGENT_") or name.startswith("BETTER_CLAUDE_"):
            monkeypatch.delenv(name, raising=False)

    captured = _patch_runtime_mcp(monkeypatch, {})

    configs = operation_cli.available_configs()
    assert set(configs) == set(operation_cli._CORE_SCRIPTS)
    inputs = captured["inputs"]
    assert inputs["bare_config"] is False
    assert inputs["user_facing"] is False
    assert inputs["active_capability_ids"] == []
    assert inputs["disabled_builtin_extensions"] == []
    assert captured["user_facing"] is False
    assert captured["bare"] is False


# --- main dispatch ----------------------------------------------------------


def test_main_list_prints_sorted_groups(monkeypatch, capsys):
    monkeypatch.setattr(operation_cli, "available_configs", lambda: {"b": {}, "a": {}})
    assert operation_cli.main(["--list"]) == 0
    assert json.loads(capsys.readouterr().out) == {"groups": ["a", "b"]}


def test_main_reads_sys_argv_when_argv_none(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["prog", "--list"])
    monkeypatch.setattr(operation_cli, "available_configs", lambda: {"g": {}})
    assert operation_cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"groups": ["g"]}


def test_main_no_args_exits(monkeypatch):
    monkeypatch.setattr(operation_cli, "available_configs", lambda: {})
    with pytest.raises(SystemExit):
        operation_cli.main([])


def test_main_invalid_group_exits(monkeypatch):
    monkeypatch.setattr(operation_cli, "available_configs", lambda: {})
    with pytest.raises(SystemExit):
        operation_cli.main(["BAD!"])


def test_main_unavailable_group_exits(monkeypatch):
    monkeypatch.setattr(operation_cli, "available_configs", lambda: {})
    with pytest.raises(SystemExit):
        operation_cli.main(["validgroup"])


def test_main_dispatches_to_exec_config(monkeypatch):
    monkeypatch.setattr(
        operation_cli, "available_configs", lambda: {"communicate": {"command": "x"}}
    )

    def fake_exec(cfg, args):
        recorded["call"] = (cfg, args)
        return 7

    recorded: dict = {}
    monkeypatch.setattr(operation_cli, "_exec_config", fake_exec)
    assert operation_cli.main(["communicate", "op", "in"]) == 7
    assert recorded["call"] == ({"command": "x"}, ["op", "in"])
