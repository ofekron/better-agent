"""Regression test for CLI lookup outside launchd's minimal PATH.

Run with:
    cd backend && .venv/bin/python scripts/test_cli_paths.py
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from cli_paths import resolve_cli_binary  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="bc-test-cli-paths-")
    old_path = os.environ.get("PATH", "")
    try:
        bin_dir = Path(tmp) / "npm-global" / "bin"
        bin_dir.mkdir(parents=True)
        codex_exe = bin_dir / "codex"
        codex_exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        codex_exe.chmod(codex_exe.stat().st_mode | stat.S_IXUSR)
        gemini_exe = bin_dir / "gemini"
        gemini_exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        gemini_exe.chmod(gemini_exe.stat().st_mode | stat.S_IXUSR)
        os.environ["PATH"] = "/usr/bin:/bin"

        codex_found = resolve_cli_binary("codex", extra_dirs=[str(bin_dir)])
        gemini_found = resolve_cli_binary("gemini", extra_dirs=[str(bin_dir)])
        ok = codex_found == str(codex_exe) and gemini_found == str(gemini_exe)
        print(
            f"{PASS if ok else FAIL} resolves CLIs from explicit non-PATH dir -- "
            f"{codex_found=} {gemini_found=}"
        )
        if os.name == "nt":
            path_dir = Path(tmp) / "path-bin"
            path_dir.mkdir()
            (path_dir / "codex").write_text("", encoding="utf-8")
            codex_win_exe = path_dir / "codex.exe"
            codex_win_exe.write_text("", encoding="utf-8")
            os.environ["PATH"] = str(path_dir)
            codex_path_found = resolve_cli_binary("codex")
            ok_win = (
                codex_path_found is not None
                and os.path.normcase(codex_path_found) == os.path.normcase(str(codex_win_exe))
            )
            print(
                f"{PASS if ok_win else FAIL} prefers Windows executable suffix -- "
                f"{codex_path_found=}"
            )
            ok = ok and ok_win
        return 0 if ok else 1
    finally:
        os.environ["PATH"] = old_path
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
