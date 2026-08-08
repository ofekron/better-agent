"""Regression test for CLI lookup outside launchd's minimal PATH.

``resolve_cli_binary`` must find ``codex``/``agy`` in an explicit non-PATH
dir (npm-global style) and, on Windows, prefer the ``.exe`` suffix from
PATH.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import installation_profile  # noqa: E402
from cli_paths import (  # noqa: E402
    _candidate_in_dir,
    _windows_path_rank,
    _windows_spawnable_path,
    resolve_cli_binary,
)

# Windows-only filesystem behavior. The Windows branches in cli_paths use
# Path() which dispatches to WindowsPath on "nt"; WindowsPath cannot be
# instantiated or operated on a non-Windows host (pathlib raises), so these
# tests run only on Windows. On other hosts the corresponding cli_paths lines
# are a justified platform exclusion (see the pragma: no cover markers there).
WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt",
    reason="Windows-only: WindowsPath filesystem accessors raise off-Windows",
)


def _make_exe(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_resolves_clis_from_explicit_non_path_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    bin_dir = tmp_path / "npm-global" / "bin"
    bin_dir.mkdir(parents=True)

    codex_exe = bin_dir / "codex"
    agy_exe = bin_dir / "agy"
    _make_exe(codex_exe)
    _make_exe(agy_exe)

    assert resolve_cli_binary("codex", extra_dirs=[str(bin_dir)]) == str(codex_exe)
    assert resolve_cli_binary("agy", extra_dirs=[str(bin_dir)]) == str(agy_exe)


@pytest.mark.skipif(os.name != "nt", reason="Windows executable-suffix behavior")
def test_prefers_windows_executable_suffix_from_path(tmp_path, monkeypatch) -> None:
    path_dir = tmp_path / "path-bin"
    path_dir.mkdir()
    (path_dir / "codex").write_text("", encoding="utf-8")
    codex_win_exe = path_dir / "codex.exe"
    codex_win_exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", str(path_dir))

    found = resolve_cli_binary("codex")
    assert found is not None
    assert os.path.normcase(found) == os.path.normcase(str(codex_win_exe))


# --- installation-profile branch -------------------------------------------------

def test_profile_pin_wins_over_filesystem(tmp_path, monkeypatch) -> None:
    """A pinned provider executable short-circuits any filesystem lookup."""
    monkeypatch.setattr(
        installation_profile,
        "pinned_provider_executable",
        lambda name: (True, "/pinned/codex"),
    )
    assert resolve_cli_binary("codex") == "/pinned/codex"


def test_respect_installation_profile_false_skips_profile(tmp_path, monkeypatch) -> None:
    """Disabling the profile flag must bypass even a pinned executable."""
    monkeypatch.setattr(
        installation_profile,
        "pinned_provider_executable",
        lambda name: (True, "/pinned/codex"),
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex_exe = bin_dir / "codex"
    _make_exe(codex_exe)
    monkeypatch.setenv("PATH", "/usr/bin")

    found = resolve_cli_binary(
        "codex", extra_dirs=[str(bin_dir)], respect_installation_profile=False
    )
    assert found == str(codex_exe)


# --- posix resolve_cli_binary tails ----------------------------------------------

def test_returns_existing_path_from_path_via_which(tmp_path, monkeypatch) -> None:
    """When shutil.which finds the binary on PATH, its path is returned as-is."""
    monkeypatch.setattr(
        installation_profile, "pinned_provider_executable", lambda name: (False, None)
    )
    bin_dir = tmp_path / "onpath"
    bin_dir.mkdir()
    codex_exe = bin_dir / "codex"
    _make_exe(codex_exe)
    monkeypatch.setenv("PATH", str(bin_dir))

    assert resolve_cli_binary("codex") == str(codex_exe)


def test_returns_none_and_continues_past_nonmatching_dirs(tmp_path, monkeypatch) -> None:
    """Non-matching extra dirs are skipped; absent everywhere yields None."""
    monkeypatch.setattr(
        installation_profile, "pinned_provider_executable", lambda name: (False, None)
    )
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.setenv("PATH", "/usr/bin")

    assert (
        resolve_cli_binary("no-such-binary-zzz", extra_dirs=[str(empty_dir)]) is None
    )


# --- _windows_spawnable_path -----------------------------------------------------

def test_windows_spawnable_path_keeps_suffix(monkeypatch) -> None:
    # Pure suffix check — no WindowsPath filesystem op, so it runs off-Windows.
    monkeypatch.setattr(os, "name", "nt")
    assert _windows_spawnable_path("C:\\bin\\codex.exe") == "C:\\bin\\codex.exe"


@WINDOWS_ONLY
def test_windows_spawnable_path_appends_executable_suffix(tmp_path) -> None:
    base = tmp_path / "codex"  # no extension
    cmd = tmp_path / "codex.cmd"
    cmd.write_text("", encoding="utf-8")

    assert _windows_spawnable_path(str(base)) == str(cmd)


@WINDOWS_ONLY
def test_windows_spawnable_path_no_match_returns_original(tmp_path) -> None:
    base = tmp_path / "codex"  # no extension, no .cmd/.exe/.bat sibling

    assert _windows_spawnable_path(str(base)) == str(base)


# --- _windows_path_rank ----------------------------------------------------------

def test_windows_path_rank_distinguishes_windowsapps() -> None:
    assert _windows_path_rank("C:\\Users\\me\\AppData\\Local\\Microsoft\\WindowsApps\\codex") == 1
    assert _windows_path_rank("C:\\WindowsApps") == 1
    assert _windows_path_rank("C:\\Program Files\\nodejs") == 0


# --- _candidate_in_dir (posix + windows branches) --------------------------------

def test_candidate_in_dir_posix_executable(tmp_path) -> None:
    exe = tmp_path / "codex"
    _make_exe(exe)

    assert _candidate_in_dir(str(tmp_path), "codex") == [str(exe)]
    assert _candidate_in_dir(str(tmp_path), "missing") == []


@WINDOWS_ONLY
def test_candidate_in_dir_windows_with_suffix(tmp_path) -> None:
    exe = tmp_path / "codex.exe"
    exe.write_text("", encoding="utf-8")

    assert _candidate_in_dir(str(tmp_path), "codex.exe") == [str(exe)]
    assert _candidate_in_dir(str(tmp_path), "missing.exe") == []


@WINDOWS_ONLY
def test_candidate_in_dir_windows_collects_suffix_and_bare(tmp_path) -> None:
    cmd = tmp_path / "y.cmd"
    cmd.write_text("", encoding="utf-8")

    assert _candidate_in_dir(str(tmp_path), "y") == [str(cmd)]

    bare = tmp_path / "z"
    bat = tmp_path / "z.bat"
    bare.write_text("", encoding="utf-8")
    bat.write_text("", encoding="utf-8")

    assert _candidate_in_dir(str(tmp_path), "z") == [str(bat), str(bare)]


# --- resolve_cli_binary windows branch -------------------------------------------

@WINDOWS_ONLY
def test_resolve_windows_ranked_candidate(tmp_path, monkeypatch) -> None:
    bin_dir = tmp_path / "winbin"
    bin_dir.mkdir()
    codex_cmd = bin_dir / "codex.cmd"
    codex_cmd.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        installation_profile, "pinned_provider_executable", lambda name: (False, None)
    )
    monkeypatch.setenv("PATH", str(bin_dir))

    found = resolve_cli_binary("codex")
    assert found == str(codex_cmd)


@WINDOWS_ONLY
def test_resolve_windows_separator_name(tmp_path, monkeypatch) -> None:
    """A name containing a path separator resolves relative to its parent dir."""
    codex_cmd = tmp_path / "codex.cmd"
    codex_cmd.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        installation_profile, "pinned_provider_executable", lambda name: (False, None)
    )
    monkeypatch.setenv("PATH", "")

    found = resolve_cli_binary(str(tmp_path / "codex"))
    assert found == str(codex_cmd)


@WINDOWS_ONLY
def test_resolve_windows_returns_none_when_absent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        installation_profile, "pinned_provider_executable", lambda name: (False, None)
    )
    monkeypatch.setenv("PATH", str(tmp_path))

    assert resolve_cli_binary("no-such-binary-zzz") is None
