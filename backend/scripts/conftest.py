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

    These files are not pytest test modules, so do not collect them. Real
    test modules (anything defining a collectable test function or class)
    always pass through. A file that fails to parse is left for pytest to
    report as a real error rather than being silently hidden here.
    """
    import ast

    name = collection_path.name
    if not name.startswith("test_") or not name.endswith(".py"):
        return None
    try:
        tree = ast.parse(collection_path.read_text(encoding="utf-8"))
    except (SyntaxError, ValueError):
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            return None
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            return None
    return True


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
