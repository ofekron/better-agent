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
SELFTEST_TIMEOUT_SECONDS = 60


def tree_hash(source_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(source_dir.rglob("*")):
        if any(part in _SKIP_DIRS for part in path.relative_to(source_dir).parts):
            continue
        if not path.is_file():
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


def needs_install(daemon_root: Path, source_dir: Path) -> bool:
    if not current_dir(daemon_root).is_dir():
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
    current = current_dir(daemon_root)
    if current.exists():
        shutil.rmtree(current)
    staging.rename(current)
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
    if last_good.exists():
        shutil.rmtree(last_good)
    shutil.copytree(current, last_good)
    meta["promoted"] = True
    write_json(daemon_root / "install.json", meta)


def rollback_to_last_good(daemon_root: Path) -> bool:
    last_good = last_good_dir(daemon_root)
    if not last_good.is_dir():
        return False
    current = current_dir(daemon_root)
    if current.exists():
        shutil.rmtree(current)
    shutil.copytree(last_good, current)
    meta = install_meta(daemon_root)
    meta["promoted"] = True
    meta["rolled_back_at"] = time.time()
    write_json(daemon_root / "install.json", meta)
    return True
