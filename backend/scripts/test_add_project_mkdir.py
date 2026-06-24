"""Locks `project_store.add_project` directory creation:

1. Adding a primary-node project whose dir doesn't exist CREATES it.
2. The returned record's path is the resolved, now-existing dir.
3. A path that can't be created (parent is a file) is rejected (None).
4. Remote-node projects are NOT mkdir'd locally.

Run with:
    cd backend && .venv/bin/python scripts/test_add_project_mkdir.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-addproj-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import project_store  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

failures = 0


def check(label: str, cond: bool) -> None:
    global failures
    print(f"{PASS if cond else FAIL} {label}")
    if not cond:
        failures += 1


_WORK = tempfile.mkdtemp(prefix="bc-test-addproj-work-")
try:
    # 1 + 2: nonexistent primary dir is created and returned resolved.
    target = os.path.join(_WORK, "newproj", "nested")
    check("dir does not exist before add", not Path(target).exists())
    rec = project_store.add_project(target)
    check("add_project returned a record", rec is not None)
    check("dir created on disk", Path(target).is_dir())
    check("record path is resolved existing dir",
          rec is not None and rec["path"] == str(Path(target).resolve()))

    # 3: path whose parent is a regular file can't be created → None.
    afile = os.path.join(_WORK, "afile")
    Path(afile).write_text("x")
    bad = os.path.join(afile, "child")
    rec_bad = project_store.add_project(bad)
    check("uncreatable path rejected (None)", rec_bad is None)
    check("no project row added for bad path",
          all(p["path"] != str(Path(bad).resolve())
              for p in project_store.list_projects()))

    # 4: remote-node project is NOT mkdir'd locally.
    remote = os.path.join(_WORK, "remote-only")
    rec_remote = project_store.add_project(remote, node_id="node-b")
    check("remote project record returned", rec_remote is not None)
    check("remote dir NOT created locally", not Path(remote).exists())
finally:
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    shutil.rmtree(_WORK, ignore_errors=True)

if failures:
    print(f"\n{failures} check(s) failed")
    sys.exit(1)
print("\nall checks passed")
