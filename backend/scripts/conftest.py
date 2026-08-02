"""Shared pytest harness for backend/scripts tests.

This conftest runs BEFORE any test module is imported for collection, so its
module body is the earliest hook we have. It engages the full test-home
protection (sentinel + temp ROOT env + deletion guard + prod-home FS lock)
before any backend module can call `paths.ba_home()` or capture a path at
module scope. See `_test_home` for why each layer exists.

A test wanting its own fresh home (instead of the session ROOT) calls
`_test_home.TestHome.acquire()` — both modes are supported.
"""

from __future__ import annotations

import ast
import os
import tempfile

import _test_home
from live_llm_test_guard import live_llm_skip_message, live_llm_tests_enabled

# Engage at import time — before any backend import in collected test modules.
# Layers 1+2 (ba_home guard + deletion guard) are always on. Layer 3 (FS lock
# on the real home) is opt-in: it gives zero-residual but also blocks a
# concurrently-running production backend from writing.
_SESSION_ROOT = tempfile.mkdtemp(prefix="ba-pytest-root-")
_test_home.engage(_SESSION_ROOT, lock=bool(os.environ.get("BA_LOCK_PROD_HOME")))

import pytest  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_llm: test requires an explicitly enabled live LLM/provider",
    )


def pytest_ignore_collect(collection_path, config):
    """Skip standalone scripts mislabeled as ``test_*.py``.

    A large part of this directory is dual-purpose: files runnable as
    ``python scripts/test_<x>.py`` (standalone assertion scripts) AND named
    to match pytest's ``test_*`` glob. The standalone ones define no
    ``def test_*`` / ``Test*`` classes, so pytest would otherwise IMPORT them
    purely to run their top-level code during collection. The guarded ones
    survive that import as a no-op; the unguarded ones do real work (spawn
    processes, mutate ``sys.modules``, call ``sys.exit``) and abort the whole
    session with "mainloop: caught unexpected SystemExit".

    These files are not pytest test modules, so do not collect them. A file
    that fails to parse is left for pytest to report as a real error rather
    than being silently hidden here.

    Some standalone runners ALSO define ``test_*`` helper functions (called
    from an unguarded top-level runner block, not under
    ``if __name__ == "__main__"``). The "has def test_*" check alone would
    let them through, but importing them fires the runner at collection time
    and raises ``SystemExit`` — which pytest escalates to INTERNALERROR and
    aborts the whole session. So the unguarded-runner check takes priority:
    any file with a module-scope exit call not guarded by the ``__main__``
    check is ignored, since pytest cannot import it side-effect-free.
    """
    name = collection_path.name
    if not name.startswith("test_") or not name.endswith(".py"):
        return None
    try:
        tree = ast.parse(collection_path.read_text(encoding="utf-8"))
    except (SyntaxError, ValueError):
        return None
    if _is_unguarded_runner(tree):
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            return None
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            return None
    return True


def _is_unguarded_runner(tree):
    """True if the module calls exit()/sys.exit() at module scope outside any
    ``if __name__ == "__main__":`` guard.

    Such a module executes its standalone runner on import, so pytest cannot
    collect it without side effects (process spawns, filesystem mutation, and
    a ``SystemExit`` that aborts the collection run). Real pytest modules
    never exit at module scope; standalone runners guard their exit behind
    the ``__main__`` check, so only the unguarded ones match.
    """
    exit_names = ("exit", "sys.exit", "os._exit")

    def _is_main_guard(node):
        return (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
            and any(
                isinstance(c, ast.Constant) and c.value == "__main__"
                for c in node.test.comparators
            )
        )

    def _exit_call_name(node):
        fn = node.func
        if isinstance(fn, ast.Name):
            return fn.id
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            return f"{fn.value.id}.{fn.attr}"
        return None

    def _walk(node, under_main):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # inside a def/class body — not module-execution scope
            this_under_main = under_main or _is_main_guard(child)
            if isinstance(child, ast.Call) and _exit_call_name(child) in exit_names:
                if not this_under_main:
                    return True
            if _walk(child, this_under_main):
                return True
        return False

    return _walk(tree, False)


def pytest_collection_modifyitems(config, items):
    if live_llm_tests_enabled():
        return

    for item in items:
        if item.get_closest_marker("live_llm"):
            item.add_marker(pytest.mark.skip(reason=live_llm_skip_message(item.name)))
        # Dual-purpose standalone-runner files build a TestClient in `__main__`
        # and pass it as `client` directly to their `test_*` functions. Under
        # pytest that `client` parameter is an unresolved fixture (no such
        # fixture exists), so the item errors at setup. These are e2e/standalone
        # tests (run via `python scripts/<file>.py`), not unit-tier pytest; skip
        # them here rather than letting the missing fixture abort the suite.
        elif "client" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.skip(
                reason="standalone-runner test: needs __main__-built client; "
                       "run via `python scripts/<file>.py`",
            ))


@pytest.fixture(autouse=True)
def _ensure_ba_home_dirs():
    import paths
    home = paths.ba_home()
    for sub in ("sessions", "runs", "ask-status", "delegate-status"):
        (home / sub).mkdir(parents=True, exist_ok=True)
