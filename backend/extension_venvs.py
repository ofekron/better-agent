"""Shared per-requirements virtualenv store for installed extensions.

Extension version snapshots do not carry their own ``.venv``. Instead each
snapshot that declares ``python_requirements`` holds a ``.venv-ref`` marker
naming a venv under ``<ba_home>/extensions/venvs/<req-hash>/`` shared by every
version (of any extension) with the same requirements + Python minor version.

Provisioning, marker writes, and GC all serialize on ``extensions/venvs.lock``
so a venv can never be garbage-collected between being built and being
referenced by a marker.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

import portable_lock
from paths import ba_home

VENV_REF_FILENAME = ".venv-ref"
_COMPLETE_MARKER = ".complete"
_HASH_RE = re.compile(r"[0-9a-f]{64}")


class VenvBuildError(RuntimeError):
    pass


def venvs_root() -> Path:
    return ba_home() / "extensions" / "venvs"


def requirements_venv_hash(requirements: list[str]) -> str:
    payload = json.dumps(
        {
            "python": f"{sys.version_info[0]}.{sys.version_info[1]}",
            "requirements": list(requirements),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def venv_python(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def venv_bin_dir(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts"
    return venv_dir / "bin"


def venv_site_packages_dir(venv_dir: Path) -> Path | None:
    if sys.platform == "win32":
        candidate = venv_dir / "Lib" / "site-packages"
        return candidate if candidate.is_dir() else None
    lib_dir = venv_dir / "lib"
    if not lib_dir.is_dir():
        return None
    for candidate in sorted(lib_dir.glob("python*/site-packages")):
        if candidate.is_dir():
            return candidate
    return None


def read_venv_ref(package_dir: Path) -> str | None:
    try:
        ref = (package_dir / VENV_REF_FILENAME).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return ref if _HASH_RE.fullmatch(ref) else None


def resolve_venv_dir(package_dir: Path) -> Path | None:
    """Shared venv referenced by a version snapshot, or None if absent/unbuilt."""
    ref = read_venv_ref(package_dir)
    if ref is None:
        return None
    venv_dir = venvs_root() / ref
    return venv_dir if _venv_ready(venv_dir) else None


def _venv_ready(venv_dir: Path) -> bool:
    return (venv_dir / _COMPLETE_MARKER).is_file() and venv_python(venv_dir).is_file()


@contextmanager
def _venvs_lock():
    lock_path = venvs_root().parent / "venvs.lock"
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_file = lock_path.open("a+b")
    try:
        portable_lock.lock_ex(lock_file.fileno())
        yield
    finally:
        try:
            portable_lock.unlock(lock_file.fileno())
        finally:
            lock_file.close()


def provision(package_dir: Path, requirements: list[str]) -> str:
    """Reference (building if needed) the shared venv for ``requirements``.

    Writes the snapshot's ``.venv-ref`` marker and builds the venv inside one
    lock critical section so concurrent GC cannot observe a built-but-yet
    unreferenced venv. Returns the requirements hash.
    """
    req_hash = requirements_venv_hash(requirements)
    venv_dir = venvs_root() / req_hash
    with _venvs_lock():
        (package_dir / VENV_REF_FILENAME).write_text(req_hash + "\n", encoding="utf-8")
        if _venv_ready(venv_dir):
            return req_hash
        if venv_dir.exists():
            # Partial leftover from a crashed build; rebuild from scratch.
            shutil.rmtree(venv_dir)
        _build(venv_dir, requirements)
    return req_hash


def _build(venv_dir: Path, requirements: list[str]) -> None:
    venv_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "venv creation failed").strip()
        raise VenvBuildError(f"extension dependency environment creation failed: {detail}")
    result = subprocess.run(
        [str(venv_python(venv_dir)), "-m", "pip", "install", *requirements],
        check=False,
        capture_output=True,
        text=True,
        timeout=10 * 60,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "pip install failed").strip()
        raise VenvBuildError(f"extension dependency install failed: {detail}")
    (venv_dir / _COMPLETE_MARKER).touch()


def prune_unreferenced(
    collect_referenced_hashes: Callable[[], set[str]],
    protected_paths: set[Path],
) -> int:
    """Delete shared venvs no on-disk snapshot marker references.

    ``collect_referenced_hashes`` runs inside the venvs lock so the reference
    scan cannot interleave with a concurrent provision. ``protected_paths``
    (e.g. paths referenced by unreconciled runs) are never deleted.
    """
    root = venvs_root()
    if not root.is_dir():
        return 0
    removed = 0
    with _venvs_lock():
        referenced = collect_referenced_hashes()
        try:
            entries = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink()]
        except OSError:
            return 0
        for entry in entries:
            if not _HASH_RE.fullmatch(entry.name) or entry.name in referenced:
                continue
            try:
                resolved = entry.resolve(strict=True)
            except OSError:
                continue
            if any(path == resolved or path.is_relative_to(resolved) for path in protected_paths):
                continue
            shutil.rmtree(entry, ignore_errors=True)
            if not entry.exists():
                removed += 1
    return removed
