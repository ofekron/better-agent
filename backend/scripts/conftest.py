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
from pathlib import Path

import _test_home
from live_llm_test_guard import live_llm_skip_message, live_llm_tests_enabled
from pytest_collection_guard import has_main_entry, should_ignore_test_module

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
    return should_ignore_test_module(collection_path, config)


def pytest_collection_modifyitems(config, items):
    if live_llm_tests_enabled():
        return

    # Cache the has-__main__-runner flag per module file across all items.
    is_runner_cache: dict[str, bool] = {}
    for item in items:
        if item.get_closest_marker("live_llm"):
            item.add_marker(pytest.mark.skip(reason=live_llm_skip_message(item.name)))
            continue
        unresolved = _standalone_runner_unresolved_params(item, is_runner_cache)
        if unresolved:
            item.add_marker(pytest.mark.skip(
                reason="standalone-runner test: parameter(s) "
                       + ", ".join(sorted(unresolved))
                       + " are supplied by the module's __main__ runner, not "
                       "pytest fixtures; run via `python scripts/<file>.py`",
            ))


def _standalone_runner_unresolved_params(item, is_runner_cache):
    """Unresolvable params on a dual-purpose standalone-runner test function.

    Dual-purpose modules expose ``test_*`` helpers that their
    ``if __name__ == "__main__"`` runner calls with arguments it builds
    (a TestClient, a ``failures`` list, a temp path, ...). Under pytest those
    arguments are not fixtures, so the item errors at setup
    (``fixture 'x' not found``). Detect that precisely and return the param
    names so the caller can skip the item — turning an inevitable setup ERROR
    into a deterministic SKIP.

    Gated on a ``__main__`` entry so a missing-fixture typo in a pure pytest
    module still surfaces as a real setup error rather than being hidden, and
    so that pollution-only collection failures (a real fixture that resolves in
    a clean process) are not papered over.
    """
    module = getattr(item, "module", None)
    source_path = getattr(module, "__file__", "") or ""
    is_runner = is_runner_cache.get(source_path)
    if is_runner is None:
        try:
            is_runner = has_main_entry(
                Path(source_path).read_text(encoding="utf-8")
            )
        except (OSError, SyntaxError, ValueError):
            is_runner = False
        is_runner_cache[source_path] = is_runner
    if not is_runner:
        return ()

    fixtureinfo = getattr(item, "_fixtureinfo", None)
    argnames = getattr(fixtureinfo, "argnames", ()) or ()
    resolved = getattr(fixtureinfo, "name2fixturedefs", {}) or {}
    return tuple(name for name in argnames if name not in resolved)


@pytest.fixture(autouse=True)
def _ensure_ba_home_dirs(request):
    import paths
    module_home = _test_home.pytest_home_for_module(request.module.__name__)
    _test_home.engage(module_home or _SESSION_ROOT)
    home = paths.ba_home()
    for sub in ("sessions", "runs", "ask-status", "delegate-status"):
        (home / sub).mkdir(parents=True, exist_ok=True)
