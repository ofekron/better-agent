"""Shared extension venv store: sharing, keying, and GC.

Proves:
  1. Two version snapshots with the same python_requirements share one venv.
  2. A requirements change provisions a distinct venv.
  3. A venv is GC'd by the prune flow once its last referencing version
     snapshot is pruned, and retained while any snapshot references it.
"""
from __future__ import annotations

import os
import sys

import _test_home
_test_home.isolate("bc-test-shared-venvs-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from pathlib import Path  # noqa: E402

import extension_store  # noqa: E402
import extension_venvs  # noqa: E402

_BUILD_CALLS: list[Path] = []


def _fake_build(venv_dir: Path, requirements: list[str]) -> None:
    _BUILD_CALLS.append(venv_dir)
    extension_venvs.venv_bin_dir(venv_dir).mkdir(parents=True, exist_ok=True)
    extension_venvs.venv_python(venv_dir).touch()
    (venv_dir / extension_venvs._COMPLETE_MARKER).touch()


extension_venvs._build = _fake_build

_EXTENSION_ID = "test.shared-venv-ext"


def _version_dir(name: str) -> Path:
    path = extension_store._install_root() / _EXTENSION_ID / "versions" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _venv_dirs() -> set[str]:
    root = extension_venvs.venvs_root()
    if not root.is_dir():
        return set()
    return {p.name for p in root.iterdir() if p.is_dir()}


def test_same_requirements_share_one_venv() -> None:
    _BUILD_CALLS.clear()
    v1 = _version_dir("v1")
    v2 = _version_dir("v2")
    h1 = extension_venvs.provision(v1, ["shared-dep==1.0"])
    h2 = extension_venvs.provision(v2, ["shared-dep==1.0"])
    assert h1 == h2, "same requirements must key the same venv"
    assert len(_BUILD_CALLS) == 1, "second provision must reuse the built venv"
    assert extension_venvs.read_venv_ref(v1) == h1
    assert extension_venvs.read_venv_ref(v2) == h1
    assert extension_venvs.resolve_venv_dir(v1) == extension_venvs.resolve_venv_dir(v2)
    assert _venv_dirs() == {h1}


def test_requirement_change_creates_new_venv() -> None:
    _BUILD_CALLS.clear()
    v3 = _version_dir("v3")
    old_hash = extension_venvs.read_venv_ref(_version_dir("v1"))
    new_hash = extension_venvs.provision(v3, ["shared-dep==2.0"])
    assert new_hash != old_hash, "changed requirements must key a new venv"
    assert len(_BUILD_CALLS) == 1
    assert _venv_dirs() == {old_hash, new_hash}


def test_venv_gc_when_last_referencing_version_pruned() -> None:
    versions_root = extension_store._install_root() / _EXTENSION_ID / "versions"
    # Rebuild the fixture from scratch: one active version + enough fallbacks
    # that the oldest (sole referencer of a doomed venv) exceeds the retention
    # window and gets pruned.
    import shutil
    shutil.rmtree(versions_root, ignore_errors=True)
    shutil.rmtree(extension_venvs.venvs_root(), ignore_errors=True)

    active = _version_dir("active")
    kept_hash = extension_venvs.provision(active, ["kept-dep==1.0"])
    fallbacks = []
    for index in range(extension_store._MAX_FALLBACK_VERSIONS + 1):
        fallback = _version_dir(f"fallback-{index}")
        fallbacks.append(fallback)
    doomed = fallbacks[0]
    doomed_hash = extension_venvs.provision(doomed, ["doomed-dep==1.0"])
    for survivor in fallbacks[1:]:
        extension_venvs.provision(survivor, ["kept-dep==1.0"])
    # Deterministic mtime order: doomed is oldest, so it falls outside the
    # newest-N retention window.
    base = 1_000_000_000
    for index, fallback in enumerate(fallbacks):
        os.utime(fallback, (base + index, base + index))
    assert _venv_dirs() == {kept_hash, doomed_hash}

    data = {
        "schema_version": extension_store.STORE_SCHEMA_VERSION,
        "extensions": {_EXTENSION_ID: {"source": {"install_path": str(active)}}},
        "deleted_extensions": {},
    }
    removed = extension_store._prune_extension_versions(data)
    assert removed == 1, f"expected exactly the oldest fallback pruned, got {removed}"
    assert not doomed.exists(), "oldest fallback version must be pruned"
    assert not (extension_venvs.venvs_root() / doomed_hash).exists(), (
        "venv must be GC'd once its last referencing version is pruned"
    )
    assert (extension_venvs.venvs_root() / kept_hash).is_dir(), (
        "venv referenced by retained versions must survive GC"
    )
    assert extension_venvs.resolve_venv_dir(active) == extension_venvs.venvs_root() / kept_hash


if __name__ == "__main__":
    test_same_requirements_share_one_venv()
    test_requirement_change_creates_new_venv()
    test_venv_gc_when_last_referencing_version_pruned()
    print("PASS extension shared venvs")
