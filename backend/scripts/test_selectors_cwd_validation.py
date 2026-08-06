#!/usr/bin/env python3
"""A session cwd is a CLI-subprocess working directory, so the selectors
PATCH fails closed like move-to-project: only an existing directory is
accepted, stored resolved to the identity the project registry uses.

Regression: the handler stored any non-empty string, so a bad path was
accepted here and only failed later at spawn time.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-selectors-cwd-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from pathlib import Path  # noqa: E402

import _test_installation  # noqa: E402

_test_installation.activate(Path(_TMP_HOME))

from starlette.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import main  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _client() -> TestClient:
    client = TestClient(main.app, client=("127.0.0.1", 50000), base_url="http://localhost:8000")
    client.headers.update({"Authorization": f"Bearer {auth.create_token('native')}"})
    return client


def _new_session(cwd: str) -> str:
    return session_manager.create(
        name="selectors-cwd", model="sonnet", cwd=cwd,
        orchestration_mode="native", source="cli",
    )["id"]


def _patch_cwd(session_id: str, cwd: str):
    return _client().patch(
        f"/api/sessions/{session_id}/selectors",
        json={"cwd": cwd, "client_id": "test"},
    )


def test_missing_directory_rejected() -> None:
    start = os.path.realpath(tempfile.mkdtemp(prefix="selectors-start-"))
    sid = _new_session(start)
    missing = os.path.join(start, "does-not-exist")
    res = _patch_cwd(sid, missing)
    assert res.status_code == 400, f"expected 400, got {res.status_code}: {res.text[:160]}"
    stored = session_manager.get(sid).get("cwd")
    assert stored == start, f"cwd must be untouched, got {stored!r}"


def test_file_path_rejected() -> None:
    start = os.path.realpath(tempfile.mkdtemp(prefix="selectors-start-"))
    sid = _new_session(start)
    a_file = os.path.join(start, "a-file.txt")
    with open(a_file, "w") as fh:
        fh.write("x")
    res = _patch_cwd(sid, a_file)
    assert res.status_code == 400, f"expected 400 for a file, got {res.status_code}"


def test_existing_directory_accepted_and_resolved() -> None:
    start = os.path.realpath(tempfile.mkdtemp(prefix="selectors-start-"))
    target = os.path.realpath(tempfile.mkdtemp(prefix="selectors-target-"))
    sid = _new_session(start)
    # Unresolved form: the handler must store the resolved path.
    unresolved = os.path.join(target, ".", "")
    res = _patch_cwd(sid, unresolved)
    assert res.status_code == 200, f"expected 200, got {res.status_code}: {res.text[:160]}"
    stored = session_manager.get(sid).get("cwd")
    assert stored == target, f"expected {target!r}, got {stored!r}"


TESTS = [
    ("missing directory rejected", test_missing_directory_rejected),
    ("file path rejected", test_file_path_rejected),
    ("existing directory accepted and resolved", test_existing_directory_accepted_and_resolved),
]


def main_() -> int:
    failures = 0
    for label, fn in TESTS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"{FAIL} {label} — {type(exc).__name__}: {exc}")
            continue
        print(f"{PASS} {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main_())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
