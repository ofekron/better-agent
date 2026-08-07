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
remember them. `isolate()` returns the resolved temp home path as a string;
new code prefers `TestHome.acquire()` for an owned handle whose
`release()` is the sole structured cleanup path. Either way the home is
removed at process exit, so a run leaves no residue behind.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

# NOTE: do NOT import `paths` (or any backend module) at module top — standalone
# test files `import _test_home` BEFORE they add the backend dir to sys.path.
# Import it lazily inside the functions that need the prod-home reference.
_GUARD_INSTALLED = False
_PROD_LOCK_COUNT = 0
_PYTEST_MODULE_HOMES: dict[str, str] = {}


def _register_importing_module_home(home: str) -> None:
    frame = sys._getframe(1)
    while frame is not None and frame.f_globals.get("__name__") == __name__:
        frame = frame.f_back
    if frame is None or frame.f_code.co_name != "<module>":
        return
    module_name = frame.f_globals.get("__name__")
    if isinstance(module_name, str) and module_name:
        _PYTEST_MODULE_HOMES[module_name] = home


def pytest_home_for_module(module_name: str) -> str | None:
    return _PYTEST_MODULE_HOMES.get(module_name)


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
# Scoped module patches (prevent cross-module leak during pytest collection)
# --------------------------------------------------------------------------- #
@contextmanager
def scoped_patches(patches):
    """Apply ``(obj, attr, value)`` patches and restore the originals on exit.

    Script-style test modules that patch shared backend callables
    (``config_store.get_provider``, ``models.available_models``, …) MUST scope
    those patches to their own tests. A bare module-level assignment leaks:
    pytest imports every module during collection before any test runs, so a
    module-top-level patch permanently replaces the callable for every later
    module — including script-style modules that execute real backend logic at
    import time. Wrap the patches in a module-scoped autouse fixture (and in a
    standalone ``main`` runner) with this helper so originals are restored the
    moment the module's own tests finish.
    """
    saved = [(obj, attr, getattr(obj, attr)) for obj, attr, _ in patches]
    for obj, attr, value in patches:
        setattr(obj, attr, value)
    try:
        yield
    finally:
        for obj, attr, original in saved:
            setattr(obj, attr, original)


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
def engage(home: str, lock: bool = False) -> str:
    # Callers invoke this before any backend import; make backend/ importable
    # regardless of the caller's own sys.path setup.
    backend_dir = str(Path(__file__).resolve().parent.parent)
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    import paths
    requested = Path(home).expanduser().resolve()
    current = paths.ba_home().resolve()
    switching = paths.is_test_mode() and current != requested
    if switching:
        _quiesce_loaded_home_owners()
    paths.engage_test_home(str(requested))
    if switching:
        _reset_loaded_home_owners()
    resolved = paths.ba_home()
    if resolved != requested:
        raise RuntimeError("test home did not resolve to the requested root")
    install_deletion_guard()
    if lock:
        lock_prod_home()
    resolved_text = str(resolved)
    _register_importing_module_home(resolved_text)
    return resolved_text


def _loaded(name: str):
    return sys.modules.get(name)


def _quiesce_loaded_home_owners() -> None:
    provider = _loaded("provider")
    if provider is not None:
        provider.reset_provider_cache_for_test_home()
    session_manager = _loaded("session_manager")
    if session_manager is not None:
        session_manager.manager.shutdown_persistence()
    session_store = _loaded("session_store")
    if session_store is not None:
        session_store.quiesce_for_test_home()
    projection = _loaded("session_queue_projection")
    runtime = _loaded("session_queue_projection_runtime")
    if projection is not None and runtime is not None:
        identity = runtime.registry.bound_identity()
        if identity is not None:
            projection.shutdown(storage_identity=identity, timeout=5.0)


def _reset_loaded_home_owners() -> None:
    session_store = _loaded("session_store")
    if session_store is not None:
        session_store.reset_for_test_home()
    session_manager = _loaded("session_manager")
    if session_manager is not None:
        session_manager.manager.reset_for_test_home()
    worker_store = _loaded("stores.worker_store")
    if worker_store is not None:
        worker_store.reset_for_test_home()
    config_store = _loaded("config_store")
    if config_store is not None:
        config_store.reset_for_test_home()
    project_update_store = _loaded("project_update_store")
    if project_update_store is not None:
        project_update_store.reset_for_test_home()


# Crash-safety: never leave the prod home immutable if the process dies
# without releasing. Registered once at import; unlock is a no-op if the
# refcount is already zero.
atexit.register(unlock_prod_home)


def isolate(prefix: str = "ba-test-", lock: bool = False) -> str:
    """Force both home env vars onto a fresh tempdir + engage all guards.

    Returns the resolved tempdir path, removed when the process exits. Call at
    the very top of a test module, BEFORE importing any backend module.

    `lock=True` sets the immutable flag on the real home (zero-residual: even
    child-process deletion fails), but it ALSO blocks a concurrently-running
    production backend from writing — only use it when no prod backend is up.
    """
    return TestHome.acquire(prefix, lock=lock).path


def _activate_installation(home: str, **kwargs) -> None:
    # Imported lazily: `_test_installation` touches backend modules, which are
    # only importable after `engage()` put backend/ on sys.path.
    import _test_installation

    _test_installation.activate(Path(home), **kwargs)


def isolate_installed(prefix: str = "ba-test-", lock: bool = False, **kwargs) -> str:
    """`isolate()` plus a committed installation profile inside that home.

    Any test that drives the ASGI app needs this: the installation admission
    middleware answers every gated route with 503 until a profile is active,
    so a bare `isolate()` home silently turns whole-app coverage into
    assertions against error responses. Extra kwargs go to
    `_test_installation.activate` (mode/provider/launcher_path).
    """
    home = isolate(prefix, lock=lock)
    _activate_installation(home, **kwargs)
    return home


class TestHome:
    """Owned test home. `release()` is the sole structured cleanup path.

    Acquire per-test for a fresh home (repoints env each time), or share one
    across a session — tests pick.

    Every acquired home is also released at process exit. A test that forgets
    to release (or that never held the handle, like `isolate()` callers) would
    otherwise strand a full state tree under the system temp dir on every run:
    the homes are megabytes each and nothing else ever collects them.
    """

    __slots__ = ("path", "_released")

    def __init__(self, path: str) -> None:
        self.path = path
        self._released = False

    @classmethod
    def acquire(cls, prefix: str = "ba-test-", lock: bool = False) -> "TestHome":
        home = tempfile.mkdtemp(prefix=prefix)
        handle = cls(engage(home, lock=lock))
        atexit.register(handle.release)
        return handle

    @classmethod
    def acquire_installed(
        cls, prefix: str = "ba-test-", lock: bool = False, **kwargs
    ) -> "TestHome":
        """`acquire()` for tests that drive the ASGI app. See `isolate_installed`."""
        handle = cls.acquire(prefix, lock=lock)
        _activate_installation(handle.path, **kwargs)
        return handle

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
        for module_name, home in tuple(_PYTEST_MODULE_HOMES.items()):
            if home == self.path:
                _PYTEST_MODULE_HOMES.pop(module_name, None)
        _ORIG_RMTREE(self.path, ignore_errors=True)
        unlock_prod_home()
