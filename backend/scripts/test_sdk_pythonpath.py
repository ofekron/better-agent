"""Promoted pytest coverage for backend/sdk_pythonpath.py.

sdk_pythonpath.py is the single source for prepending a checkout's sdk/ onto
PYTHONPATH so spawned processes can import `better_agent_sdk` (a bare package,
not an installed distribution). It was previously only exercised by the desktop
standalone runner desktop/test_backend_sdk_pythonpath.py (not pytest-collected),
so it measured 0% in the unit tier. This module ports the pure env-builder
coverage into the pytest unit tier and closes every branch.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from sdk_pythonpath import apply_sdk_pythonpath, sdk_pythonpath  # noqa: E402


def _checkout_with_sdk(tmp_path: Path, name: str = "ck") -> Path:
    checkout = tmp_path / name
    (checkout / "sdk").mkdir(parents=True)
    return checkout


# --------------------------------------------------------------------------- #
# sdk_pythonpath
# --------------------------------------------------------------------------- #

def test_returns_existing_unchanged_when_sdk_dir_absent(tmp_path):
    checkout = tmp_path / "no-sdk"  # no sdk/ subdir
    assert sdk_pythonpath(checkout, "/other") == "/other"
    assert sdk_pythonpath(checkout, "") == ""


def test_prepends_sdk_when_existing_empty(tmp_path):
    checkout = _checkout_with_sdk(tmp_path)
    assert sdk_pythonpath(checkout, "") == str(checkout / "sdk")


def test_prepends_sdk_before_existing(tmp_path):
    checkout = _checkout_with_sdk(tmp_path)
    got = sdk_pythonpath(checkout, "/other")
    assert got == str(checkout / "sdk") + os.pathsep + "/other"


def test_does_not_duplicate_when_sdk_already_present(tmp_path):
    checkout = _checkout_with_sdk(tmp_path)
    existing = str(checkout / "sdk") + os.pathsep + "/other"
    assert sdk_pythonpath(checkout, existing) == existing


# --------------------------------------------------------------------------- #
# apply_sdk_pythonpath
# --------------------------------------------------------------------------- #

def test_apply_sets_pythonpath_when_sdk_present(tmp_path):
    checkout = _checkout_with_sdk(tmp_path)
    env: dict[str, str] = {}
    result = apply_sdk_pythonpath(env, checkout)
    assert result is env  # mutated in place AND returned
    assert env["PYTHONPATH"] == str(checkout / "sdk")


def test_apply_merges_with_existing_pythonpath(tmp_path):
    checkout = _checkout_with_sdk(tmp_path)
    env = {"PYTHONPATH": "/other", "HOME": "/tmp"}
    apply_sdk_pythonpath(env, checkout)
    assert env["PYTHONPATH"] == str(checkout / "sdk") + os.pathsep + "/other"
    assert env["HOME"] == "/tmp"  # unrelated keys preserved


def test_apply_leaves_env_untouched_when_sdk_absent(tmp_path):
    checkout = tmp_path / "no-sdk"
    env: dict[str, str] = {}
    result = apply_sdk_pythonpath(env, checkout)
    assert result is env
    assert "PYTHONPATH" not in env


def test_apply_preserves_empty_existing_when_sdk_absent(tmp_path):
    checkout = tmp_path / "no-sdk"
    env = {"PYTHONPATH": "/kept"}
    apply_sdk_pythonpath(env, checkout)
    assert env["PYTHONPATH"] == "/kept"
