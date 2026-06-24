"""Locks `file_browser.list_directories` non-existent-path behavior:

1. An existing dir → exists=True, entries listed.
2. Empty path → home dir, exists=True (no bounce surprise).
3. An explicitly-typed nonexistent path → returned AS-IS with
   exists=False and no entries (NOT silently bounced to home).
4. A path pointing at a file → exists=False (can't browse into a file).
5. Picker create helpers create files/directories and reject bad targets.

Run with:
    cd backend && .venv/bin/python scripts/test_browse_nonexistent.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import file_browser  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

failures = 0


def check(label: str, cond: bool) -> None:
    global failures
    print(f"{PASS if cond else FAIL} {label}")
    if not cond:
        failures += 1


_WORK = tempfile.mkdtemp(prefix="bc-test-browse-")
try:
    sub = os.path.join(_WORK, "child")
    os.mkdir(sub)

    # 1: existing dir.
    r = file_browser.list_directories(_WORK)
    check("existing dir exists=True", r["exists"] is True)
    check("existing dir lists child", any(e["name"] == "child" for e in r["entries"]))

    # 2: empty path → home, exists True.
    r_home = file_browser.list_directories("")
    check("empty path exists=True", r_home["exists"] is True)
    check("empty path is home", r_home["path"] == Path.home().as_posix())

    # 3: nonexistent path returned as-is, NOT bounced to home.
    missing = os.path.join(_WORK, "nope", "deeper")
    r_missing = file_browser.list_directories(missing)
    check("missing path exists=False", r_missing["exists"] is False)
    check("missing path returned as-is (not home)",
          r_missing["path"] == Path(missing).resolve().as_posix())
    check("missing path has no entries", r_missing["entries"] == [])

    # 4: a file path can't be browsed.
    afile = os.path.join(_WORK, "afile.txt")
    Path(afile).write_text("x")
    r_file = file_browser.list_directories(afile)
    check("file path exists=False", r_file["exists"] is False)

    # 5: create helpers.
    created_file = os.path.join(_WORK, "created.txt")
    cf = file_browser.create_file(created_file)
    check("create_file creates an empty file", Path(created_file).is_file())
    check("create_file returns normalized path", cf["path"] == Path(created_file).resolve().as_posix())

    created_dir = os.path.join(_WORK, "created-dir")
    cd = file_browser.create_directory(created_dir)
    check("create_directory creates a directory", Path(created_dir).is_dir())
    check("create_directory returns directory type", cd["type"] == "directory")

    try:
        file_browser.create_file(created_file)
        duplicate_rejected = False
    except FileExistsError:
        duplicate_rejected = True
    check("create_file rejects duplicates", duplicate_rejected)

    try:
        file_browser.create_directory(os.path.join(_WORK, "missing-parent", "child"))
        missing_parent_rejected = False
    except FileNotFoundError:
        missing_parent_rejected = True
    check("create_directory rejects missing parent", missing_parent_rejected)
finally:
    shutil.rmtree(_WORK, ignore_errors=True)

if failures:
    print(f"\n{failures} check(s) failed")
    sys.exit(1)
print("\nall checks passed")
