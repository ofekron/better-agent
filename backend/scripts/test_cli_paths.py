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

from cli_paths import resolve_cli_binary  # noqa: E402


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
