"""Single entry point for test state-home isolation + destructive guards.

WHY THIS EXISTS: a leaked or inherited real `BETTER_AGENT_HOME` once let a
test's `shutil.rmtree` delete the developer's real `~/.better-claude`. This
module makes that structurally impossible through three independent layers:

  1. `paths.ba_home()` refuses any state root at or under a production home
     (`~/.better-claude` / `~/.better-agent`) when `BETTER_AGENT_TEST_MODE`
     is set (see paths.assert_state_root_safe).
  2. This module wraps the Python deletion primitives (`shutil.rmtree`,
     `os.remove`, `os.unlink`, `pathlib.Path.unlink`) to reject any target
     resolving under that same prod home — covers deletion of a captured or
     hardcoded path that never went through `ba_home()`.
  3. `lock_prod_home()` sets the platform immutable flag on the real home so
     even out-of-process / child-process deletion fails.

Layers 1 and 2 are installed automatically by `isolate()` (standalone tests)
and by the conftest module body (pytest runs), so callers do not have to
remember them. `isolate()` returns the temp home path as a string (legacy
contract); new code prefers `TestHome.acquire()` for an owned handle whose
`release()` is the sole structured cleanup path.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

# NOTE: do NOT import `paths` (or any backend module) at module top — standalone
# test files `import _test_home` BEFORE they add the backend dir to sys.path.
# Import it lazily inside the functions that need the prod-home reference.
_GUARD_INSTALLED = False
_PROD_LOCK_COUNT = 0


# --------------------------------------------------------------------------- #
# Layer 2: deletion guard
# --------------------------------------------------------------------------- #
_ORIG_RMTREE = shutil.rmtree
_ORIG_OS_REMOVE = os.remove
_ORIG_PATH_UNLINK = Path.unlink


def _resolves_under_prod(target: object) -> bool:
    import paths
    if not paths.is_test_mode():
        return False
    # Fail CLOSED: a target we cannot resolve (bytes, bad type, null bytes) is
    # ambiguous, and ambiguity about deleting the prod home must deny, not
    # proceed. Returning False here would let `shutil.rmtree(b"~/.better-claude")`
    # slip past the guard and delete the real home.
    try:
        resolved = Path(target).expanduser().resolve()  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError) as exc:
        raise RuntimeError(
            f"test deletion guard: cannot resolve target {target!r}; "
            f"refusing to proceed (fail-closed)"
        ) from exc
    for prod in paths.prod_state_roots():
        prod_r = prod.resolve()
        if resolved == prod_r or prod_r in resolved.parents:
            return True
    return False


def _guarded_rmtree(path, *args, **kwargs):
    if _resolves_under_prod(path):
        raise RuntimeError(
            f"test deletion guard: refusing rmtree under prod home ({path})"
        )
    return _ORIG_RMTREE(path, *args, **kwargs)


def _guarded_os_remove(path, *args, **kwargs):
    if _resolves_under_prod(path):
        raise RuntimeError(
            f"test deletion guard: refusing remove under prod home ({path})"
        )
    return _ORIG_OS_REMOVE(path, *args, **kwargs)


def _guarded_path_unlink(self, *args, **kwargs):
    if _resolves_under_prod(self):
        raise RuntimeError(
            f"test deletion guard: refusing unlink under prod home ({self})"
        )
    return _ORIG_PATH_UNLINK(self, *args, **kwargs)


def install_deletion_guard() -> None:
    """Idempotently wrap the deletion primitives. Safe to call from every
    test entry point — patches once per process."""
    global _GUARD_INSTALLED
    if _GUARD_INSTALLED:
        return
    shutil.rmtree = _guarded_rmtree
    os.remove = _guarded_os_remove
    os.unlink = _guarded_os_remove
    Path.unlink = _guarded_path_unlink
    _GUARD_INSTALLED = True


# --------------------------------------------------------------------------- #
# Layer 3: filesystem-level immutable flag on the real home
# --------------------------------------------------------------------------- #
def _chflags(path: Path, immutable: bool) -> None:
    if os.name != "posix":
        return
    import subprocess

    flag = "uchg" if immutable else "nouchg"
    # macOS: chflags. Linux: chattr (+i / -i) if the fs supports it.
    tool, prefix = ("chflags", "") if shutil.which("chflags") else (
        "chattr",
        "+i" if immutable else "-i",
    )
    if not shutil.which(tool):
        return
    arg = f"{prefix}{flag}" if tool == "chattr" else flag
    subprocess.run([tool, arg, str(path)], check=False, capture_output=True)


def lock_prod_home() -> None:
    """Best-effort: make the real home immutable so even child processes can't
    delete it while tests run. Reference-counted so nested entry points don't
    unlock early. Note: this also blocks a concurrently-running production
    backend from writing — only engage during test runs."""
    global _PROD_LOCK_COUNT
    if _PROD_LOCK_COUNT > 0:
        _PROD_LOCK_COUNT += 1
        return
    import paths
    prod = paths.user_home()
    for name in (paths._DEFAULT_STATE_DIR, paths._DEFAULT_ALIAS_DIR):
        candidate = prod / name
        if candidate.exists() and not candidate.is_symlink():
            _chflags(candidate, immutable=True)
            break
    _PROD_LOCK_COUNT = 1


def unlock_prod_home() -> None:
    global _PROD_LOCK_COUNT
    if _PROD_LOCK_COUNT == 0:
        return
    _PROD_LOCK_COUNT -= 1
    if _PROD_LOCK_COUNT > 0:
        return
    import paths
    prod = paths.user_home()
    for name in (paths._DEFAULT_STATE_DIR, paths._DEFAULT_ALIAS_DIR):
        candidate = prod / name
        if candidate.exists() and not candidate.is_symlink():
            _chflags(candidate, immutable=False)


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def engage(home: str, lock: bool = False) -> None:
    import paths
    paths.engage_test_home(home)
    install_deletion_guard()
    if lock:
        lock_prod_home()


# Crash-safety: never leave the prod home immutable if the process dies
# without releasing. Registered once at import; unlock is a no-op if the
# refcount is already zero.
atexit.register(unlock_prod_home)


def isolate(prefix: str = "ba-test-", lock: bool = False) -> str:
    """Force both home env vars onto a fresh tempdir + engage all guards.

    Returns the tempdir path (string, legacy contract). Call at the very top
    of a test module, BEFORE importing any backend module.

    `lock=True` sets the immutable flag on the real home (zero-residual: even
    child-process deletion fails), but it ALSO blocks a concurrently-running
    production backend from writing — only use it when no prod backend is up.
    """
    home = tempfile.mkdtemp(prefix=prefix)
    engage(home, lock=lock)
    return home


class TestHome:
    """Owned test home. `release()` is the sole structured cleanup path.

    Acquire per-test for a fresh home (repoints env each time), or share one
    across a session — tests pick.
    """

    __slots__ = ("path", "_released")

    def __init__(self, path: str) -> None:
        self.path = path
        self._released = False

    @classmethod
    def acquire(cls, prefix: str = "ba-test-", lock: bool = False) -> "TestHome":
        home = tempfile.mkdtemp(prefix=prefix)
        engage(home, lock=lock)
        return cls(home)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        # Defense against a public-constructor misuse (TestHome(prod_path)):
        # refuse before the unwrapped delete. Legit temp paths pass.
        if _resolves_under_prod(self.path):
            raise RuntimeError(
                f"TestHome.release refusing prod-rooted path ({self.path})"
            )
        _ORIG_RMTREE(self.path, ignore_errors=True)
        unlock_prod_home()
