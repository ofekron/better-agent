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

# Engage at import time — before any backend import in collected test modules.
# Layers 1+2 (ba_home guard + deletion guard) are always on. Layer 3 (FS lock
# on the real home) is opt-in: it gives zero-residual but also blocks a
# concurrently-running production backend from writing.
_SESSION_ROOT = tempfile.mkdtemp(prefix="ba-pytest-root-")
_test_home.engage(_SESSION_ROOT, lock=bool(os.environ.get("BA_LOCK_PROD_HOME")))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _ensure_ba_home_dirs():
    import paths
    home = paths.ba_home()
    for sub in ("sessions", "runs", "ask-status", "delegate-status"):
        (home / sub).mkdir(parents=True, exist_ok=True)
