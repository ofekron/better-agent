"""Owner test for `paths.py` — the state-directory source of truth.

The Windows-only ctypes ACL machinery (`_WindowsSecurity` and its three
helpers) is pragma-excluded in `paths.py`: it calls `WinDLL`, which is absent
on the POSIX test platform, and faking ctypes would give no real confidence in
a security-critical ACL path. Everything else here is reachable on POSIX and
is covered with real assertions, not line-touchers.

The shared `conftest.py` autouse fixture already engages an isolated
`BETTER_AGENT_HOME` tempdir per test, so `ba_home()` never touches the real
home. The autouse fixture below only resets the in-process caches/globals that
`paths` memoizes across calls.
"""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import paths


@pytest.fixture(autouse=True)
def _reset_paths_caches(monkeypatch):
    """Drop memoized home/user state between tests and restore os.name."""
    paths._USER_HOME = None
    paths.reset_home_cache()
    paths._WINDOWS_SECURITY = None
    paths._WINDOWS_CURRENT_USER_SID = None
    saved_name = paths.os.name
    yield
    paths._USER_HOME = None
    paths.reset_home_cache()
    paths._WINDOWS_SECURITY = None
    paths._WINDOWS_CURRENT_USER_SID = None
    paths.os.name = saved_name


# ---- fakes for the private-file/directory dispatch --------------------------


class _File:
    """Path-like POSIX file: lstat + chmod, no junction."""

    def __init__(self, mode: int, uid: int) -> None:
        self._mode = mode
        self._uid = uid
        self.chmods: list[int] = []

    def lstat(self) -> SimpleNamespace:
        return SimpleNamespace(st_mode=stat.S_IFREG | self._mode, st_uid=self._uid)

    def is_junction(self) -> bool:
        return False

    def chmod(self, mode: int) -> None:
        self.chmods.append(mode)
        self._mode = mode


class _Dir:
    """Path-like POSIX directory."""

    def __init__(self, mode: int, uid: int) -> None:
        self._mode = mode
        self._uid = uid

    def lstat(self) -> SimpleNamespace:
        return SimpleNamespace(st_mode=stat.S_IFDIR | self._mode, st_uid=self._uid)

    def is_junction(self) -> bool:
        return False


# ---- user_home / expand_user_path ------------------------------------------


def test_user_home_is_absolute_and_cached():
    first = paths.user_home()
    assert first.is_absolute()
    # Cached: a sentinel stamp is returned verbatim on the next call.
    paths._USER_HOME = Path("/sentinel-cached")
    assert paths.user_home() == Path("/sentinel-cached")


def test_expand_user_path_tilde_and_subpath(monkeypatch):
    monkeypatch.setattr(paths, "user_home", lambda: Path("/real/home"))
    assert paths.expand_user_path("~") == Path("/real/home")
    assert paths.expand_user_path("~/data/x") == Path("/real/home/data/x")


def test_expand_user_path_plain_and_envvar(monkeypatch):
    monkeypatch.setattr(paths, "user_home", lambda: Path("/real/home"))
    monkeypatch.setenv("BA_EXP_TEST", "/var/exp")
    assert paths.expand_user_path("$BA_EXP_TEST/sub") == Path("/var/exp/sub")
    assert paths.expand_user_path("/abs/path") == Path("/abs/path")


# ---- is_test_mode / assert_state_root_safe ---------------------------------


@pytest.mark.parametrize("val,expected", [
    ("", False), ("0", False), ("false", False), ("no", False),
    ("1", True), ("yes", True), ("TRUE", True),
])
def test_is_test_mode_truthiness(monkeypatch, val, expected):
    if val == "":
        monkeypatch.delenv(paths._TEST_MODE_ENV, raising=False)
    else:
        monkeypatch.setenv(paths._TEST_MODE_ENV, val)
    assert paths.is_test_mode() is expected


def test_assert_state_root_safe_no_op_outside_test_mode(monkeypatch):
    monkeypatch.delenv(paths._TEST_MODE_ENV, raising=False)
    # Must not raise even for the production root.
    paths.assert_state_root_safe(paths.user_home() / ".better-claude")


def test_assert_state_root_safe_rejects_prod_root_in_test_mode(monkeypatch):
    monkeypatch.setenv(paths._TEST_MODE_ENV, "1")
    with pytest.raises(RuntimeError, match="refusing state root"):
        paths.assert_state_root_safe(paths.user_home() / ".better-claude")
    with pytest.raises(RuntimeError):
        paths.assert_state_root_safe(paths.user_home() / ".better-agent")


def test_assert_state_root_safe_accepts_tempdir_in_test_mode(monkeypatch, tmp_path):
    monkeypatch.setenv(paths._TEST_MODE_ENV, "1")
    paths.assert_state_root_safe(tmp_path)  # must not raise


# ---- _install_private_umask ------------------------------------------------


def test_install_private_umask_tightens_and_noops_on_windows(monkeypatch):
    if paths.os.name == "nt":
        pytest.skip("umask is POSIX-only")
    # No bits beyond 0o077 -> no re-umask; ends at the private umask.
    prev = os.umask(0o022)
    try:
        paths._install_private_umask()
        assert os.umask(0o022) & 0o077 == 0o077  # group/other bits cleared
    finally:
        os.umask(prev)

    # Owner bit (outside 0o077) present -> re-umask preserves it.
    prev = os.umask(0o100)
    try:
        paths._install_private_umask()
        assert os.umask(0o022) == 0o177  # 0o100 | 0o077
    finally:
        os.umask(prev)

    # Windows dispatch no-ops without touching the umask.
    monkeypatch.setattr(paths.os, "name", "nt")
    os.umask(0o027)
    paths._install_private_umask()
    assert os.umask(0o027) == 0o027  # read-back returns 0o027, then restores


# ---- _require_non_redirecting_path OSError branch --------------------------


def test_require_non_redirecting_path_translates_oserror():
    class _Broken:
        def lstat(self):
            raise FileNotFoundError("gone")

        def is_junction(self):
            return False

    with pytest.raises(PermissionError, match="unavailable"):
        paths._require_non_redirecting_path(_Broken(), directory=False)


# ---- make_private_file / require_private_file ------------------------------


def test_make_private_file_posix_chmods(monkeypatch):
    monkeypatch.setattr(paths.os, "name", "posix")
    monkeypatch.setattr(paths.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(paths, "_pwd", object())
    target = _File(0o644, 1000)
    paths.make_private_file(target)
    assert target.chmods == [0o600]


def test_make_private_file_windows_applies_then_verifies(monkeypatch):
    monkeypatch.setattr(paths.os, "name", "nt")
    applied: list[tuple[object, bool]] = []
    state = {"acl": False}

    def acl_check(_p):
        return state["acl"]

    monkeypatch.setattr(paths, "windows_path_has_private_acl", acl_check)

    # Applying secures the file -> verify passes.
    def securing_apply(path, *, directory):
        applied.append((path, directory))
        state["acl"] = True

    monkeypatch.setattr(paths, "_set_windows_private_acl", securing_apply)
    target = _File(0o600, 0)
    paths.make_private_file(target)
    assert applied == [(target, False)]

    # Apply does not secure -> fail closed.
    state["acl"] = False
    applied.clear()
    monkeypatch.setattr(
        paths, "_set_windows_private_acl",
        lambda path, *, directory: applied.append((path, directory)),
    )
    with pytest.raises(PermissionError, match="verification failed"):
        paths.make_private_file(_File(0o600, 0))
    assert applied and applied[0][1] is False


def test_require_private_file_windows_branch(monkeypatch):
    monkeypatch.setattr(paths.os, "name", "nt")
    monkeypatch.setattr(paths, "windows_path_has_private_acl", lambda _p: True)
    paths.require_private_file(_File(0o600, 0))  # secure -> no raise
    monkeypatch.setattr(paths, "windows_path_has_private_acl", lambda _p: False)
    with pytest.raises(PermissionError, match="verification failed"):
        paths.require_private_file(_File(0o600, 0))


def test_require_private_file_posix_safe_and_unsafe(monkeypatch):
    monkeypatch.setattr(paths.os, "name", "posix")
    monkeypatch.setattr(paths.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(paths, "_pwd", object())
    paths.require_private_file(_File(0o600, 1000))  # safe
    with pytest.raises(PermissionError, match="unsafe"):
        paths.require_private_file(_File(0o644, 1000))  # group/other bits
    with pytest.raises(PermissionError, match="unsafe"):
        paths.require_private_file(_File(0o600, 1001))  # foreign owner


# ---- windows_path_has_private_acl ------------------------------------------


def test_windows_path_has_private_acl_posix_returns_false():
    assert paths.windows_path_has_private_acl(Path("/anything")) is False


def test_windows_path_has_private_acl_dispatches_and_fails_closed(monkeypatch):
    monkeypatch.setattr(paths.os, "name", "nt")

    class _Sec:
        def has_private_acl(self, path, *, user_sid):
            return True

    monkeypatch.setattr(paths, "_windows_security", lambda: _Sec())
    monkeypatch.setattr(paths, "_windows_current_user_sid", lambda: "S-1")
    assert paths.windows_path_has_private_acl(Path("/x")) is True

    def _boom():
        raise OSError("no acl")

    monkeypatch.setattr(paths, "_windows_security", _boom)
    assert paths.windows_path_has_private_acl(Path("/x")) is False


# ---- make_private_directory / require_private_directory --------------------


def test_make_private_directory_windows_apply_exception_fails_closed(monkeypatch):
    monkeypatch.setattr(paths.os, "name", "nt")
    monkeypatch.setattr(paths, "windows_path_has_private_acl", lambda _p: False)

    def _raise(path, *, directory):
        raise OSError("acl engine down")

    monkeypatch.setattr(paths, "_set_windows_private_acl", _raise)
    with pytest.raises(PermissionError, match="ACL update failed"):
        paths.make_private_directory(_Dir(0o700, 0))


def test_require_private_directory_windows_branch(monkeypatch):
    monkeypatch.setattr(paths.os, "name", "nt")
    monkeypatch.setattr(paths, "windows_path_has_private_acl", lambda _p: True)
    paths.require_private_directory(_Dir(0o700, 0))
    monkeypatch.setattr(paths, "windows_path_has_private_acl", lambda _p: False)
    with pytest.raises(PermissionError, match="verification failed"):
        paths.require_private_directory(_Dir(0o700, 0))


def test_require_private_directory_posix_safe_and_unsafe(monkeypatch):
    monkeypatch.setattr(paths.os, "name", "posix")
    monkeypatch.setattr(paths.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(paths, "_pwd", object())
    paths.require_private_directory(_Dir(0o700, 1000))  # safe
    with pytest.raises(PermissionError, match="unsafe"):
        paths.require_private_directory(_Dir(0o750, 1000))  # group bit set
    with pytest.raises(PermissionError, match="unsafe"):
        paths.require_private_directory(_Dir(0o700, 1001))  # foreign owner


# ---- _env_home / _default_home / _ensure_default_alias ----------------------


def test_env_home_rejects_relative(monkeypatch):
    monkeypatch.delenv(paths._PRIMARY_HOME_ENV, raising=False)
    monkeypatch.delenv(paths._LEGACY_HOME_ENV, raising=False)
    assert paths._env_home(paths._PRIMARY_HOME_ENV) is None
    monkeypatch.setenv(paths._PRIMARY_HOME_ENV, "relative/path")
    with pytest.raises(ValueError, match="absolute path"):
        paths._env_home(paths._PRIMARY_HOME_ENV)
    monkeypatch.setenv(paths._PRIMARY_HOME_ENV, "/abs/home")
    assert paths._env_home(paths._PRIMARY_HOME_ENV) == Path("/abs/home")


def test_default_home_prefers_alias_when_legacy_absent(monkeypatch, tmp_path):
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: fake_home))
    legacy = fake_home / paths._DEFAULT_STATE_DIR
    alias = fake_home / paths._DEFAULT_ALIAS_DIR
    alias.mkdir()  # alias present, legacy absent -> alias
    assert paths._default_home() == alias
    legacy.mkdir()  # legacy present -> legacy wins
    assert paths._default_home() == legacy


def test_ensure_default_alias_skips_windows_and_creates_symlink(monkeypatch, tmp_path):
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: fake_home))
    root = tmp_path / "stateroot"
    root.mkdir()
    alias = fake_home / paths._DEFAULT_ALIAS_DIR

    monkeypatch.setattr(paths.os, "name", "nt")
    paths._ensure_default_alias(root)
    assert not alias.exists()  # skipped on Windows

    monkeypatch.setattr(paths.os, "name", "posix")
    paths._ensure_default_alias(root)  # creates alias symlink
    assert alias.is_symlink()
    paths._ensure_default_alias(root)  # idempotent when alias exists
    assert alias.is_symlink()


# ---- reset_home_cache / ba_home / _resolve_home_uncached -------------------


def test_reset_home_cache_clears_memoization():
    paths.ba_home()  # populate cache + secured-roots
    assert paths._HOME_CACHE
    paths.reset_home_cache()
    assert paths._HOME_CACHE == {}
    assert paths._SECURED_ROOTS == set()


def test_ba_home_reresolves_on_env_change(monkeypatch, tmp_path):
    a = paths.ba_home()
    monkeypatch.setenv(paths._PRIMARY_HOME_ENV, str(tmp_path / "other"))
    paths.reset_home_cache()
    b = paths.ba_home()
    assert b != a
    assert b == (tmp_path / "other")


def test_ba_home_concurrent_first_resolve_serializes(monkeypatch):
    """Late callers hit the in-lock double-check rather than re-resolving."""
    paths.reset_home_cache()
    barrier = threading.Barrier(10)
    results: list[Path] = []
    once = {"done": False}
    origin = paths.make_private_directory

    def slow_first(path):
        if not once["done"]:
            once["done"] = True
            time.sleep(0.05)  # stretch only the first critical section
        return origin(path)

    monkeypatch.setattr(paths, "make_private_directory", slow_first)

    def call():
        barrier.wait()
        results.append(paths.ba_home())

    threads = [threading.Thread(target=call) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 10
    assert len(set(results)) == 1  # all resolve to one root


def test_resolve_home_uncached_creates_alias_for_default(monkeypatch, tmp_path):
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.delenv(paths._PRIMARY_HOME_ENV, raising=False)
    monkeypatch.delenv(paths._LEGACY_HOME_ENV, raising=False)
    paths.reset_home_cache()
    root = paths._resolve_home_uncached()
    assert root == fake_home / paths._DEFAULT_STATE_DIR
    assert (fake_home / paths._DEFAULT_ALIAS_DIR).is_symlink()


def test_resolve_home_uncached_secures_root_only_once(monkeypatch, tmp_path):
    monkeypatch.setenv(paths._PRIMARY_HOME_ENV, str(tmp_path / "h"))
    paths.reset_home_cache()
    calls = {"n": 0}
    origin = paths.make_private_directory

    def spy(path):
        calls["n"] += 1
        return origin(path)

    monkeypatch.setattr(paths, "make_private_directory", spy)
    first = paths._resolve_home_uncached()
    second = paths._resolve_home_uncached()  # already secured -> skip re-securing
    assert first == second
    assert calls["n"] == 1


# ---- _record_resolve_ms ----------------------------------------------------


def test_record_resolve_ms_records_and_swallows(monkeypatch):
    import perf

    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(perf, "record", lambda name, ms: calls.append((name, ms)))
    paths._record_resolve_ms(7.5)
    assert calls == [("paths.ba_home.resolve", 7.5)]

    def boom(name, ms):
        raise RuntimeError("perf down")

    monkeypatch.setattr(perf, "record", boom)
    paths._record_resolve_ms(1.0)  # must not raise


# ---- encode_cwd ------------------------------------------------------------


def test_encode_cwd_replaces_separators_and_underscore():
    assert paths.encode_cwd("/foo/bar_baz") == "-foo-bar-baz"
    # Root collapses to a single dash (one separator -> one dash).
    assert paths.encode_cwd("/") == "-"


def test_encode_cwd_resolves_relative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    expected = str(tmp_path.resolve()).replace("/", "-").replace("_", "-")
    assert paths.encode_cwd(".") == expected


# ---- resolve_provider_config_dir / resolve_claude_config_dir ---------------


def test_resolve_provider_config_dir_variants(monkeypatch):
    monkeypatch.setattr(paths, "user_home", lambda: Path("/real/home"))
    monkeypatch.setenv("HOME", "/spoofed")  # must be ignored; user_home wins
    assert paths.resolve_provider_config_dir("$HOME/.cfg") == Path("/real/home/.cfg")
    assert paths.resolve_provider_config_dir("${HOME}/.cfg") == Path("/real/home/.cfg")
    assert paths.resolve_provider_config_dir(".codex-work") == Path("/real/home/.codex-work")
    assert paths.resolve_provider_config_dir("/etc/codex") == Path("/etc/codex")


def test_resolve_claude_config_dir_aliases_provider_resolver(monkeypatch):
    monkeypatch.setattr(paths, "user_home", lambda: Path("/real/home"))
    assert paths.resolve_claude_config_dir(".claude-zai") == paths.resolve_provider_config_dir(".claude-zai")


# ---- claude_projects_root_for_session --------------------------------------


def test_claude_projects_root_precedence(monkeypatch):
    import config_store

    env_default = Path.home() / ".claude" / "projects"

    # No provider, no env -> default.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert paths.claude_projects_root_for_session({}) == env_default

    # Env var set -> resolves through it.
    monkeypatch.setattr(paths, "user_home", lambda: Path("/real/home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/.claude-zai")
    assert paths.claude_projects_root_for_session({}) == Path("/real/home/.claude-zai/projects")

    # Provider record with config_dir wins over env.
    monkeypatch.setattr(
        config_store, "get_provider",
        lambda pid: {"config_dir": "/provider/cfg"} if pid == "claude" else None,
    )
    assert paths.claude_projects_root_for_session({"provider_id": "claude"}) == Path("/provider/cfg/projects")

    # Provider record with empty config_dir -> falls through to env.
    monkeypatch.setattr(config_store, "get_provider", lambda pid: {"config_dir": "  "})
    assert paths.claude_projects_root_for_session({"provider_id": "claude"}) == Path("/real/home/.claude-zai/projects")

    # Provider id present but no record -> falls through to env.
    monkeypatch.setattr(config_store, "get_provider", lambda pid: None)
    assert paths.claude_projects_root_for_session({"provider_id": "ghost"}) == Path("/real/home/.claude-zai/projects")
