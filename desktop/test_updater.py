"""Test desktop/updater.py — auto-update decision core (GUI-independent).

Run with:
    backend/.venv/bin/python desktop/test_updater.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

# Isolate state dir BEFORE importing any backend module (project rule).
_TMP_HOME = tempfile.mkdtemp(prefix="bc-updater-test-")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ["BETTER_CLAUDE_HOME"] = _TMP_HOME

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import updater  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


class _FakeClient:
    """Stand-in for tufup.client.Client."""

    def __init__(self, *, new_version):
        self._new = new_version

    def check_for_updates(self):
        if self._new is None:
            return None
        return type("TargetMeta", (), {"version": self._new})()


def _set_url(url):
    if url is None:
        os.environ.pop("BA_UPDATE_URL", None)
    else:
        os.environ["BA_UPDATE_URL"] = url


def test_disabled_when_url_unset() -> bool:
    _set_url(None)
    orig_root = updater._bundled_root
    orig_url = updater._bundled_update_url
    updater._bundled_root = lambda: None
    updater._bundled_update_url = lambda: None
    calls = []
    try:
        if not updater.is_enabled():
            return False
        if updater.update_base_url() != "http://127.0.0.1:8000/api/desktop/updates":
            return False
        if updater.check() is not None:
            return False
        t = updater.start_background_check(lambda v: calls.append(v))
        if t is None:
            return False
        t.join(timeout=5)
        return calls == []
    finally:
        updater._bundled_root = orig_root
        updater._bundled_update_url = orig_url


def test_base_url_strips_trailing_slash() -> bool:
    _set_url("http://127.0.0.1:9000/")
    return updater.update_base_url() == "http://127.0.0.1:9000"


def test_update_identity_matches_better_agent_artifacts() -> bool:
    return updater.APP_NAME == "BetterAgent"


def test_bundled_update_url_used_when_env_unset() -> bool:
    _set_url(None)
    orig = updater._bundled_update_url
    updater._bundled_update_url = lambda: "http://primary.local:8000/api/desktop/updates"
    try:
        return updater.update_base_url() == "http://primary.local:8000/api/desktop/updates"
    finally:
        updater._bundled_update_url = orig


def test_check_nonfatal_on_error() -> bool:
    """A failing check must not raise — it logs and returns None so the
    app still launches (the chosen non-fatal failure policy)."""
    _set_url("http://127.0.0.1:9000")
    orig_root = updater._ensure_trusted_root
    orig_build = updater._build_client

    def _boom():
        raise RuntimeError("host down")

    updater._ensure_trusted_root = lambda: True
    updater._build_client = _boom
    try:
        return updater.check() is None  # must not raise
    finally:
        updater._ensure_trusted_root = orig_root
        updater._build_client = orig_build


def test_check_returns_new_version() -> bool:
    _set_url("http://127.0.0.1:9000")
    orig_root = updater._ensure_trusted_root
    orig_build = updater._build_client
    updater._ensure_trusted_root = lambda: True
    updater._build_client = lambda: _FakeClient(new_version="1.2.3")
    try:
        return updater.check() == "1.2.3"
    finally:
        updater._ensure_trusted_root = orig_root
        updater._build_client = orig_build


def test_check_none_when_up_to_date() -> bool:
    _set_url("http://127.0.0.1:9000")
    orig_root = updater._ensure_trusted_root
    orig_build = updater._build_client
    updater._ensure_trusted_root = lambda: True
    updater._build_client = lambda: _FakeClient(new_version=None)
    try:
        return updater.check() is None
    finally:
        updater._ensure_trusted_root = orig_root
        updater._build_client = orig_build


def test_disabled_when_no_trusted_root() -> bool:
    """Enabled URL but no trusted root metadata → dormant (None)."""
    _set_url("http://127.0.0.1:9000")
    return updater.check() is None  # tempdir home has no root.json


def test_background_check_invokes_callback() -> bool:
    _set_url("http://127.0.0.1:9000")
    orig_check = updater.check
    updater.check = lambda: "9.9.9"
    got = []
    done = threading.Event()

    def _cb(v):
        got.append(v)
        done.set()

    try:
        t = updater.start_background_check(_cb)
        if t is None:
            return False
        done.wait(timeout=5)
        t.join(timeout=5)
        return got == ["9.9.9"]
    finally:
        updater.check = orig_check


TESTS = [
    ("defaults to backend-hosted update URL when BA_UPDATE_URL unset", test_disabled_when_url_unset),
    ("update_base_url strips trailing slash", test_base_url_strips_trailing_slash),
    ("update identity matches Better Agent artifacts", test_update_identity_matches_better_agent_artifacts),
    ("bundled primary-host update URL is used when env unset", test_bundled_update_url_used_when_env_unset),
    ("check() is non-fatal on error", test_check_nonfatal_on_error),
    ("check() returns new version string", test_check_returns_new_version),
    ("check() is None when up to date", test_check_none_when_up_to_date),
    ("dormant when no trusted root", test_disabled_when_no_trusted_root),
    ("background check invokes callback", test_background_check_invokes_callback),
]


def main_run() -> int:
    failed = 0
    for name, fn in TESTS:
        try:
            ok = fn()
        except Exception as e:
            ok = False
            import traceback
            traceback.print_exc()
            print(f"  exception: {e}")
        print(f"{PASS if ok else FAIL}  {name}")
        if not ok:
            failed += 1
    print()
    print(f"{failed} of {len(TESTS)} test(s) FAILED" if failed
          else f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
