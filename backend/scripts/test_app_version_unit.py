"""Unit coverage for app_version.py.

Pure-logic commit-sha/dirty detection with fully mocked git subprocesses.
No real git state is relied upon; subprocess.check_output / subprocess.run
are patched so detection is deterministic and isolated from the host repo.
"""
from __future__ import annotations

import importlib
import subprocess

import app_version


# --------------------------------------------------------------- clean_commit_sha

def test_clean_commit_sha_non_string_returns_empty():
    assert app_version.clean_commit_sha(None) == ""
    assert app_version.clean_commit_sha(1234567) == ""


def test_clean_commit_sha_invalid_shapes_return_empty():
    assert app_version.clean_commit_sha("") == ""
    assert app_version.clean_commit_sha("   ") == ""
    assert app_version.clean_commit_sha("abc123") == ""      # too short (6)
    assert app_version.clean_commit_sha("g123456") == ""      # non-hex char
    assert app_version.clean_commit_sha("z" * 40) == ""       # non-hex, length ok


def test_clean_commit_sha_valid_normalizes():
    assert app_version.clean_commit_sha("ABCDEF0") == "abcdef0"
    assert app_version.clean_commit_sha("  0123ABC  ") == "0123abc"
    assert app_version.clean_commit_sha("a" * 40) == "a" * 40
    assert app_version.clean_commit_sha("1" * 7) == "1" * 7


# --------------------------------------------------------------------- _git_output

def test_git_output_strips_trailing_whitespace(monkeypatch):
    captured = {}

    def fake_check_output(cmd, **kwargs):
        captured["cmd"] = cmd
        return "deadbeef\n"

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)
    assert app_version._git_output("rev-parse", "HEAD") == "deadbeef"
    assert captured["cmd"] == ["git", "rev-parse", "HEAD"]


def test_git_output_failure_returns_empty(monkeypatch):
    def boom(cmd, **kwargs):
        raise FileNotFoundError("no git")

    monkeypatch.setattr(subprocess, "check_output", boom)
    assert app_version._git_output("rev-parse", "HEAD") == ""


def test_git_output_timeout_returns_empty(monkeypatch):
    monkeypatch.setattr(
        subprocess, "check_output",
        lambda cmd, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, 2)),
    )
    assert app_version._git_output("status") == ""


# ------------------------------------------------------------- _detect_commit_sha

_SHA_ENV_KEYS = (
    "BETTER_AGENT_COMMIT_SHA", "BETTER_CLAUDE_COMMIT_SHA", "GIT_COMMIT_SHA",
)


def _clear_sha_env(monkeypatch):
    for key in _SHA_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_detect_commit_sha_env_priority_agent_over_legacy(monkeypatch):
    _clear_sha_env(monkeypatch)
    monkeypatch.setenv("BETTER_AGENT_COMMIT_SHA", "aaaaaaa")
    monkeypatch.setenv("BETTER_CLAUDE_COMMIT_SHA", "bbbbbbb")
    assert app_version._detect_commit_sha() == "aaaaaaa"


def test_detect_commit_sha_legacy_over_generic(monkeypatch):
    _clear_sha_env(monkeypatch)
    monkeypatch.setenv("BETTER_CLAUDE_COMMIT_SHA", "ccccccc")
    monkeypatch.setenv("GIT_COMMIT_SHA", "ddddddd")
    assert app_version._detect_commit_sha() == "ccccccc"


def test_detect_commit_sha_generic_var(monkeypatch):
    _clear_sha_env(monkeypatch)
    monkeypatch.setenv("GIT_COMMIT_SHA", "eeeeeee")
    assert app_version._detect_commit_sha() == "eeeeeee"


def test_detect_commit_sha_invalid_env_falls_through_to_git(monkeypatch):
    _clear_sha_env(monkeypatch)
    monkeypatch.setenv("BETTER_AGENT_COMMIT_SHA", "not-a-sha")
    monkeypatch.setattr(app_version, "_git_output", lambda *a: "fffffff")
    assert app_version._detect_commit_sha() == "fffffff"


def test_detect_commit_sha_no_env_uses_git(monkeypatch):
    _clear_sha_env(monkeypatch)
    monkeypatch.setattr(app_version, "_git_output", lambda *a: "1111111")
    assert app_version._detect_commit_sha() == "1111111"


def test_detect_commit_sha_nothing_available_returns_empty(monkeypatch):
    _clear_sha_env(monkeypatch)
    monkeypatch.setattr(app_version, "_git_output", lambda *a: "")
    assert app_version._detect_commit_sha() == ""


# ----------------------------------------------------------------- _detect_dirty

class _FakeProc:
    def __init__(self, returncode: int):
        self.returncode = returncode


def _patch_git(monkeypatch, tracked_rc: int, index_rc: int):
    calls = {"tracked": 0, "index": 0}

    def fake_run(cmd, **kwargs):
        is_cached = "--cached" in cmd
        if is_cached:
            calls["index"] += 1
            return _FakeProc(index_rc)
        calls["tracked"] += 1
        return _FakeProc(tracked_rc)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_detect_dirty_false_when_no_sha(monkeypatch):
    _clear_sha_env(monkeypatch)
    monkeypatch.setattr(app_version, "_git_output", lambda *a: "")
    assert app_version._detect_dirty() is False


def test_detect_dirty_false_when_tree_and_index_clean(monkeypatch):
    _clear_sha_env(monkeypatch)
    monkeypatch.setattr(app_version, "_git_output", lambda *a: "abcdef0")
    calls = _patch_git(monkeypatch, tracked_rc=0, index_rc=0)
    assert app_version._detect_dirty() is False
    assert calls["tracked"] == 1 and calls["index"] == 1


def test_detect_dirty_true_when_tracked_dirty(monkeypatch):
    _clear_sha_env(monkeypatch)
    monkeypatch.setattr(app_version, "_git_output", lambda *a: "abcdef0")
    calls = _patch_git(monkeypatch, tracked_rc=1, index_rc=0)
    assert app_version._detect_dirty() is True
    assert calls["tracked"] == 1


def test_detect_dirty_true_when_index_dirty(monkeypatch):
    _clear_sha_env(monkeypatch)
    monkeypatch.setattr(app_version, "_git_output", lambda *a: "abcdef0")
    calls = _patch_git(monkeypatch, tracked_rc=0, index_rc=1)
    assert app_version._detect_dirty() is True
    assert calls["index"] == 1


def test_detect_dirty_false_on_subprocess_failure(monkeypatch):
    _clear_sha_env(monkeypatch)
    monkeypatch.setattr(app_version, "_git_output", lambda *a: "abcdef0")

    def boom(cmd, **kwargs):
        raise OSError("git gone")

    monkeypatch.setattr(subprocess, "run", boom)
    assert app_version._detect_dirty() is False


# ------------------------------------------------------------------- _repo_root

def test_repo_root_is_repo_dir():
    root = app_version._repo_root()
    # app_version.py lives at <repo>/backend/app_version.py, so parents[1] is
    # the repo root (used as the git subprocess cwd). The module sits under
    # backend/ relative to it.
    assert (root / "backend" / "app_version.py").exists()


# ----------------------------------------------- module-level cache (reload path)

def test_module_level_cache_and_accessors(monkeypatch):
    _clear_sha_env(monkeypatch)
    monkeypatch.setenv("BETTER_AGENT_COMMIT_SHA", "feedface")
    # Clean tree: both checks rc 0.
    _patch_git(monkeypatch, tracked_rc=0, index_rc=0)

    importlib.reload(app_version)
    try:
        assert app_version.current_commit_sha() == "feedface"
        assert app_version.current_dirty() is False
        assert app_version.current_build_info() == {
            "commit_sha": "feedface", "dirty": False,
        }
    finally:
        # Restore module state for other tests in the session.
        importlib.reload(app_version)
