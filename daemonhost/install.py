"""Selftest-gated install of supervisor-daemon copies.

Layout under ba_home()/daemons/<ext>/<name>/:
    current/     the running copy
    last_good/   promoted only after a full healthy cycle (crash rollback)
    install.json {source_hash, installed_at, promoted}

A candidate replaces ``current`` only after its ``--selftest`` exits 0, so an
agent-committed regression on the active line cannot brick the control plane.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from daemonhost.jsonio import read_json, write_json

_SKIP_DIRS = {"__pycache__", ".venv", "node_modules", ".git"}
_COPY_MARKER = ".daemon-copy.json"
SELFTEST_TIMEOUT_SECONDS = 60


def tree_hash(source_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(source_dir.rglob("*")):
        if any(part in _SKIP_DIRS for part in path.relative_to(source_dir).parts):
            continue
        if not path.is_file():
            continue
        if path.name == _COPY_MARKER:
            continue
        digest.update(str(path.relative_to(source_dir)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def install_meta(daemon_root: Path) -> dict[str, Any]:
    return read_json(daemon_root / "install.json")


def current_dir(daemon_root: Path) -> Path:
    return daemon_root / "current"


def last_good_dir(daemon_root: Path) -> Path:
    return daemon_root / "last_good"


def previous_dir(daemon_root: Path) -> Path:
    return daemon_root / "previous"


def seal_copy(copy_dir: Path) -> None:
    write_json(copy_dir / _COPY_MARKER, {"content_hash": tree_hash(copy_dir)})


def copy_is_valid(copy_dir: Path) -> bool:
    marker = read_json(copy_dir / _COPY_MARKER)
    return marker.get("content_hash") == tree_hash(copy_dir)


def _recover_predecessor(target: Path, predecessor: Path) -> bool:
    if target.is_dir() and copy_is_valid(target):
        return True
    shutil.rmtree(target, ignore_errors=True)
    if predecessor.is_dir() and copy_is_valid(predecessor):
        predecessor.rename(target)
        return True
    shutil.rmtree(predecessor, ignore_errors=True)
    return False


def recover_current(daemon_root: Path) -> bool:
    current = current_dir(daemon_root)
    if current.is_dir() and copy_is_valid(current):
        return True
    shutil.rmtree(current, ignore_errors=True)
    previous = previous_dir(daemon_root)
    if previous.is_dir() and copy_is_valid(previous):
        previous.rename(current)
        return True
    shutil.rmtree(previous, ignore_errors=True)
    last_good = last_good_dir(daemon_root)
    if last_good.is_dir() and copy_is_valid(last_good):
        _replace_tree(
            last_good,
            current,
            daemon_root / "recovery_staging",
            previous_dir(daemon_root),
        )
        return True
    return False


def _replace_tree(source: Path, target: Path, staging: Path, predecessor: Path) -> None:
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(source, staging)
    if tree_hash(staging) != tree_hash(source):
        shutil.rmtree(staging, ignore_errors=True)
        raise OSError("staged daemon copy failed verification")
    seal_copy(staging)
    if not copy_is_valid(staging):
        shutil.rmtree(staging, ignore_errors=True)
        raise OSError("staged daemon copy seal failed verification")
    shutil.rmtree(predecessor, ignore_errors=True)
    if target.exists():
        target.rename(predecessor)
    try:
        staging.rename(target)
    except OSError:
        _recover_predecessor(target, predecessor)
        raise
    shutil.rmtree(predecessor, ignore_errors=True)


def needs_install(daemon_root: Path, source_dir: Path) -> bool:
    if not current_dir(daemon_root).is_dir():
        return True
    if not copy_is_valid(current_dir(daemon_root)):
        return True
    return install_meta(daemon_root).get("source_hash") != tree_hash(source_dir)


def run_selftest(copy_dir: Path, module: str, python_exe: str, env: dict[str, str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [python_exe, "-m", module, "--selftest"],
            cwd=copy_dir,
            env={**env, "PYTHONPATH": str(copy_dir)},
            capture_output=True,
            text=True,
            timeout=SELFTEST_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()[-500:]
    return True, ""


def install(daemon_root: Path, source_dir: Path, module: str, python_exe: str, env: dict[str, str]) -> tuple[bool, str]:
    """Stage the source, selftest it, and atomically swap it into current/.

    Returns (installed, error). On selftest failure the existing current/ is
    left untouched.
    """
    staging = daemon_root / "staging"
    if staging.exists():
        shutil.rmtree(staging)
    daemon_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, staging, ignore=shutil.ignore_patterns(*_SKIP_DIRS))
    ok, error = run_selftest(staging, module, python_exe, env)
    if not ok:
        shutil.rmtree(staging, ignore_errors=True)
        return False, error or "selftest failed"
    seal_copy(staging)
    current = current_dir(daemon_root)
    previous = previous_dir(daemon_root)
    if previous.exists():
        shutil.rmtree(previous)
    if current.exists():
        current.rename(previous)
    try:
        staging.rename(current)
    except OSError as exc:
        if previous.is_dir() and not current.exists():
            previous.rename(current)
        return False, str(exc)
    shutil.rmtree(previous, ignore_errors=True)
    write_json(
        daemon_root / "install.json",
        {"source_hash": tree_hash(source_dir), "installed_at": time.time(), "promoted": False},
    )
    return True, ""


def promote_last_good(daemon_root: Path) -> None:
    """Called after the current copy completed a full healthy cycle."""
    meta = install_meta(daemon_root)
    if meta.get("promoted"):
        return
    current = current_dir(daemon_root)
    if not current.is_dir():
        return
    last_good = last_good_dir(daemon_root)
    _recover_predecessor(last_good, daemon_root / "last_good_previous")
    _replace_tree(
        current,
        last_good,
        daemon_root / "last_good_staging",
        daemon_root / "last_good_previous",
    )
    meta["promoted"] = True
    write_json(daemon_root / "install.json", meta)


def rollback_to_last_good(daemon_root: Path) -> bool:
    last_good = last_good_dir(daemon_root)
    _recover_predecessor(last_good, daemon_root / "last_good_previous")
    if not last_good.is_dir():
        return False
    current = current_dir(daemon_root)
    _replace_tree(
        last_good,
        current,
        daemon_root / "rollback_staging",
        previous_dir(daemon_root),
    )
    meta = install_meta(daemon_root)
    meta["promoted"] = True
    meta["rolled_back_at"] = time.time()
    write_json(daemon_root / "install.json", meta)
    return True
