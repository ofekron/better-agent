"""Locks: a `bare_config` session (provisioned machine-completion worker /
TestApe-isolated run) must NOT auto-register its cwd as a user project,
while a normal session MUST.

Regression for the bug where the requirements extension's
provisioned worker (cwd = the extension's own install dir) leaked a
hash-named project into the user's project list.

Run with:
    cd backend && .venv/bin/python scripts/test_bare_config_no_project.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-bareproj-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import project_store  # noqa: E402
import session_store  # noqa: E402
import session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

failures = 0


def check(label: str, cond: bool) -> None:
    global failures
    print(f"{PASS if cond else FAIL} {label}")
    if not cond:
        failures += 1


def _project_paths() -> set[str]:
    return {p["path"] for p in project_store.list_projects()}


_WORK = tempfile.mkdtemp(prefix="bc-test-bareproj-work-")
try:
    # Predicate-level checks (single source of truth).
    check("predicate: bare_config cwd is NOT eligible",
          not session_store.should_auto_register_project(
              {"cwd": _WORK, "bare_config": True}))
    check("predicate: normal cwd IS eligible",
          session_store.should_auto_register_project(
              {"cwd": _WORK, "bare_config": False}))
    check("predicate: no cwd is NOT eligible",
          not session_store.should_auto_register_project(
              {"cwd": "", "bare_config": False}))

    # End-to-end through session_manager.create (drives
    # _ensure_project_for_session).
    bare_dir = os.path.join(_WORK, "ext-install-dir")
    Path(bare_dir).mkdir(parents=True, exist_ok=True)
    session_manager.manager.create(
        name="worker:requirements:pipeline-operator",
        cwd=bare_dir,
        bare_config=True,
    )
    check("bare_config session did NOT register a project",
          str(Path(bare_dir).resolve()) not in _project_paths())

    normal_dir = os.path.join(_WORK, "real-project")
    Path(normal_dir).mkdir(parents=True, exist_ok=True)
    session_manager.manager.create(
        name="Session normal",
        cwd=normal_dir,
        bare_config=False,
    )
    check("normal session DID register a project",
          str(Path(normal_dir).resolve()) in _project_paths())
finally:
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    shutil.rmtree(_WORK, ignore_errors=True)

if failures:
    print(f"\n{failures} check(s) failed")
    sys.exit(1)
print("\nall checks passed")
