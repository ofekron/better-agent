from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def clean_commit_sha(value: object) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if not _COMMIT_RE.fullmatch(cleaned):
        return ""
    return cleaned.lower()


def _git_output(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=_repo_root(),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
    except Exception:
        return ""


def _detect_commit_sha() -> str:
    for key in ("BETTER_AGENT_COMMIT_SHA", "BETTER_CLAUDE_COMMIT_SHA", "GIT_COMMIT_SHA"):
        value = clean_commit_sha(os.environ.get(key))
        if value:
            return value
    return clean_commit_sha(_git_output("rev-parse", "HEAD"))


def _detect_dirty() -> bool:
    if not _detect_commit_sha():
        return False
    try:
        tracked_clean = subprocess.run(
            ["git", "diff", "--quiet"],
            cwd=_repo_root(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        ).returncode == 0
        index_clean = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=_repo_root(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        ).returncode == 0
    except Exception:
        return False
    return not (tracked_clean and index_clean)


_PROCESS_COMMIT_SHA = _detect_commit_sha()
_PROCESS_DIRTY = _detect_dirty()


def current_commit_sha() -> str:
    return _PROCESS_COMMIT_SHA


def repository_head_commit_sha() -> str:
    return clean_commit_sha(_git_output("rev-parse", "HEAD"))


def current_dirty() -> bool:
    return _PROCESS_DIRTY


def current_build_info() -> dict:
    return {
        "commit_sha": _PROCESS_COMMIT_SHA,
        "dirty": _PROCESS_DIRTY,
    }
