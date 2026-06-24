"""Capture the user's real PATH so bundled CLIs (`claude`, `gemini`,
`node`) resolve when the app is launched from the OS shell, not a terminal.

macOS: a Finder/launchd-launched `.app` inherits a minimal PATH
(`/usr/bin:/bin:/usr/sbin:/sbin`), not the interactive login-shell PATH —
so we ask the login shell. Windows: a normally-launched app DOES inherit
the user PATH, but a PATH entry added after the session started (or by an
installer) may be missing; we read the persistent PATH from the registry
and merge it with the process PATH.

The desktop shell calls `capture_login_path()` once at startup and passes
the result as the PATH for the backend child process, which propagates it
(via `Provider.build_env`) to every runner subprocess the backend spawns.
"""

from __future__ import annotations

import os
import subprocess
import sys

_FALLBACK_SHELL = "/bin/zsh"  # macOS default login shell since Catalina


def _capture_login_path_macos() -> str:
    """PATH as the user's interactive login shell sees it.

    Runs `<shell> -ilc 'printf %s "$PATH"'` — interactive AND login so the
    shell sources both its profile and rc files (e.g. zsh's `.zprofile`
    AND `.zshrc`), catching a PATH set in either. Falls back to the
    current process PATH if the shell cannot be run or yields nothing."""
    shell = os.environ.get("SHELL") or _FALLBACK_SHELL
    try:
        result = subprocess.run(
            [shell, "-ilc", 'printf %s "$PATH"'],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return os.environ.get("PATH", "")
    captured = (result.stdout or "").strip()
    return captured or os.environ.get("PATH", "")


def _merge_path_entries(chunks: list[str]) -> str:
    """Join PATH fragments into one PATH, de-duplicating entries while
    preserving first-seen order. Empty entries are dropped."""
    seen: set[str] = set()
    out: list[str] = []
    for chunk in chunks:
        for entry in (chunk or "").split(os.pathsep):
            if entry and entry not in seen:
                seen.add(entry)
                out.append(entry)
    return os.pathsep.join(out)


def _read_windows_path_entries() -> list[str]:
    """The persistent PATH values from the user and system registry hives
    (expanded). Build-verified on Windows; returns [] on any read error."""
    import winreg  # Windows-only stdlib

    chunks: list[str] = []
    for hive, subkey in (
        (winreg.HKEY_CURRENT_USER, "Environment"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ):
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
                chunks.append(os.path.expandvars(value))
        except OSError:
            continue
    return chunks


def _capture_path_windows() -> str:
    """The process PATH merged with the persistent registry PATH, so a
    just-installed `node`/`claude` resolves even if it post-dates the
    user's logon session."""
    chunks = [os.environ.get("PATH", "")] + _read_windows_path_entries()
    return _merge_path_entries(chunks)


def capture_login_path() -> str:
    """Return the user's real PATH for spawning the backend child."""
    if sys.platform == "darwin":
        return _capture_login_path_macos()
    if os.name == "nt":
        return _capture_path_windows()
    return os.environ.get("PATH", "")
