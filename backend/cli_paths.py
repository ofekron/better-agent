from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_CLI_DIRS = (
    "/usr/local/lib/npm-global/bin",
    "~/.npm-global/bin",
    "~/.local/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
)


_WINDOWS_EXECUTABLE_SUFFIXES = (".cmd", ".exe", ".bat")


def _windows_spawnable_path(path: str) -> str:
    if os.name != "nt":
        return path
    p = Path(path)
    if p.suffix:
        return path
    for suffix in _WINDOWS_EXECUTABLE_SUFFIXES:
        candidate = p.with_name(p.name + suffix)
        if candidate.is_file():
            return str(candidate)
    return path


def _windows_path_rank(path: str) -> int:
    lowered = path.lower()
    if "\\windowsapps\\" in lowered or lowered.endswith("\\windowsapps"):
        return 1
    return 0


def _candidate_in_dir(raw_dir: str, name: str) -> list[str]:
    candidate = Path(os.path.expanduser(raw_dir)) / name
    if os.name != "nt":
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return [str(candidate)]
        return []

    if candidate.suffix:
        return [str(candidate)] if candidate.is_file() else []

    candidates: list[str] = []
    for suffix in _WINDOWS_EXECUTABLE_SUFFIXES:
        suffixed = candidate.with_name(candidate.name + suffix)
        if suffixed.is_file():
            candidates.append(str(suffixed))
    if candidate.is_file():
        candidates.append(str(candidate))
    return candidates


def resolve_cli_binary(
    name: str,
    extra_dirs: Iterable[str] = (),
    *,
    respect_installation_profile: bool = True,
) -> Optional[str]:
    if respect_installation_profile:
        import installation_profile

        pinned, path = installation_profile.pinned_provider_executable(name)
        if pinned:
            return path
    if os.name == "nt":
        path_dirs = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
        candidates: list[str] = []
        if any(sep in name for sep in (os.sep, "/", "\\")):
            candidates.extend(_candidate_in_dir(str(Path(name).parent), Path(name).name))
        else:
            for raw_dir in [*path_dirs, *extra_dirs, *DEFAULT_CLI_DIRS]:
                candidates.extend(_candidate_in_dir(raw_dir, name))
        if candidates:
            return sorted(candidates, key=_windows_path_rank)[0]
        return None

    found = shutil.which(name)
    if found:
        return _windows_spawnable_path(found)

    for raw_dir in [*extra_dirs, *DEFAULT_CLI_DIRS]:
        candidate = Path(os.path.expanduser(raw_dir)) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return _windows_spawnable_path(str(candidate))
    return None
