"""Promoted pytest coverage for desktop/shell_env.py — login-shell PATH capture.

shell_env.py captures the user's real PATH so bundled CLIs (claude/agy/node)
resolve when the app is launched from the OS shell, not a terminal. It was
previously only exercised by desktop/test_shell_env.py (a standalone runner,
NOT pytest-collected) and desktop/supervisor.py at call time, so it measured
0% in the unit tier. This module ports that coverage into pytest and closes
every POSIX-reachable branch. The Windows registry bodies
(_read_windows_path_entries / _capture_path_windows) are `# pragma: no cover`
in the source: genuinely winreg-only, untestable on a POSIX host.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop"))

import shell_env  # noqa: E402
from shell_env import (  # noqa: E402
    _FALLBACK_SHELL,
    _capture_login_path_macos,
    _merge_path_entries,
    capture_login_path,
)


def _completed(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


# ---------------------------------------------------------------------------
# _merge_path_entries
# ---------------------------------------------------------------------------


class TestMergePathEntries:
    def test_dedups_preserving_first_seen_order(self):
        sep = os.pathsep
        chunks = [
            sep.join(["a", "b", ""]),     # process PATH (trailing empty)
            sep.join(["b", "c"]),         # duplicate b dropped, c added
            sep.join(["c", "d"]),         # duplicate c dropped, d added
        ]
        assert _merge_path_entries(chunks) == sep.join(["a", "b", "c", "d"])

    def test_empty_inputs_yield_empty_path(self):
        assert _merge_path_entries([]) == ""
        assert _merge_path_entries(["", ""]) == ""

    def test_whitespace_only_entry_is_kept(self):
        # A whitespace-only fragment is a non-empty string, so it is kept as a
        # real entry (the filter drops empties, not blanks).
        assert _merge_path_entries(["   "]) == "   "

    def test_none_chunk_guarded_as_empty(self):
        """The `(chunk or "")` guard must tolerate a None fragment without
        raising — defense for callers that may hand back None."""
        sep = os.pathsep
        assert _merge_path_entries([None, f"x{sep}y"]) == f"x{sep}y"


# ---------------------------------------------------------------------------
# _capture_login_path_macos — subprocess.run monkeypatched, no real shell spawn.
# ---------------------------------------------------------------------------


class TestCaptureLoginPathMacos:
    def test_strips_and_returns_nonempty_stdout(self, monkeypatch):
        calls = []

        def fake_run(argv, *a, **kw):
            calls.append(argv)
            return _completed("  /usr/local/bin:/usr/bin  ")

        monkeypatch.setattr(shell_env.subprocess, "run", fake_run)
        monkeypatch.delenv("SHELL", raising=False)

        assert _capture_login_path_macos() == "/usr/local/bin:/usr/bin"
        # SHELL unset -> the documented macOS fallback login shell.
        assert calls[0][0] == _FALLBACK_SHELL

    def test_honors_shell_env_when_set(self, monkeypatch):
        calls = []

        def fake_run(argv, *a, **kw):
            calls.append(argv)
            return _completed("/opt/bin")

        monkeypatch.setattr(shell_env.subprocess, "run", fake_run)
        monkeypatch.setenv("SHELL", "/bin/bash")

        assert _capture_login_path_macos() == "/opt/bin"
        assert calls[0][0] == "/bin/bash"

    def test_empty_stdout_falls_back_to_process_path(self, monkeypatch):
        monkeypatch.setattr(shell_env.subprocess, "run", lambda *a, **kw: _completed("   "))
        monkeypatch.setenv("PATH", "/proc/bin")
        monkeypatch.delenv("SHELL", raising=False)

        assert _capture_login_path_macos() == "/proc/bin"

    def test_subprocess_error_falls_back_to_process_path(self, monkeypatch):
        def raise_oserror(*a, **kw):
            raise OSError("no shell")

        monkeypatch.setattr(shell_env.subprocess, "run", raise_oserror)
        monkeypatch.setenv("PATH", "/proc/bin")

        assert _capture_login_path_macos() == "/proc/bin"

    def test_subprocess_subprocesserror_falls_back_to_process_path(self, monkeypatch):
        def raise_subproc(*a, **kw):
            raise subprocess.SubprocessError("timed out")

        monkeypatch.setattr(shell_env.subprocess, "run", raise_subproc)
        monkeypatch.setenv("PATH", "/proc/bin")

        assert _capture_login_path_macos() == "/proc/bin"

    def test_empty_stdout_with_no_path_env_yields_empty(self, monkeypatch):
        monkeypatch.setattr(shell_env.subprocess, "run", lambda *a, **kw: _completed(""))
        monkeypatch.delenv("PATH", raising=False)

        assert _capture_login_path_macos() == ""


# ---------------------------------------------------------------------------
# capture_login_path — platform dispatch (darwin / nt / else).
# ---------------------------------------------------------------------------


class TestCaptureLoginPathDispatch:
    def test_darwin_dispatches_to_macos_capture(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(shell_env, "_capture_login_path_macos", lambda: "DARWIN_PATH")

        assert capture_login_path() == "DARWIN_PATH"

    def test_posix_else_branch_returns_process_path(self, monkeypatch):
        # sys.platform != "darwin", os.name != "nt" -> final env-PATH return.
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(os, "name", "posix")
        monkeypatch.setenv("PATH", "/linux/bin")

        assert capture_login_path() == "/linux/bin"

    def test_posix_else_branch_defaults_to_empty_when_no_path(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(os, "name", "posix")
        monkeypatch.delenv("PATH", raising=False)

        assert capture_login_path() == ""

    def test_nt_dispatches_to_windows_capture(self, monkeypatch):
        # darwin branch false, os.name == "nt" -> windows capture (stubbed).
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setattr(shell_env, "_capture_path_windows", lambda: "WIN_PATH")

        assert capture_login_path() == "WIN_PATH"
