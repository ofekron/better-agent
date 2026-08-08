"""Locks that the native-import job processes sessions newest-first and
that a `limit` cap therefore keeps the most recent N.

Runs `_run_import` with `enumerate_native_sessions` / `import_session`
stubbed so it exercises only the ordering + cap logic, no real ingest.
"""

from __future__ import annotations

import os
import sys

import _test_home

_test_home.isolate("bc-test-native-import-order-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import native_import as ni  # noqa: E402


def _sess(nid: str, created_at: str) -> ni.NativeSession:
    return ni.NativeSession(
        provider_id="p", provider_kind="claude", native_id=nid,
        jsonl_path=f"/tmp/{nid}.jsonl", created_at=created_at,
    )


# Scrambled input order; created_at out of order, one unknown ("").
_SCRAMBLED = [
    _sess("mid", "2026-03-01T00:00:00Z"),
    _sess("newest", "2026-06-01T00:00:00Z"),
    _sess("unknown", ""),
    _sess("oldest", "2026-01-01T00:00:00Z"),
    _sess("second", "2026-05-01T00:00:00Z"),
]
_EXPECTED_DESC = ["newest", "second", "mid", "oldest", "unknown"]


def test_imports_newest_first_without_limit() -> None:
    seen: list[str] = []
    patches = [
        (ni, "enumerate_native_sessions", lambda *a, **k: list(_SCRAMBLED)),
        (ni, "import_session", lambda sess: seen.append(sess.native_id)),
        (ni, "already_imported_keys", lambda: set()),
    ]
    with _test_home.scoped_patches(patches):
        st = ni.JobStatus()
        ni._run_import(st, None, None)
    assert seen == _EXPECTED_DESC, f"order {seen} != {_EXPECTED_DESC}"
    assert st.imported == 5, f"imported {st.imported} != 5"
    assert st.status == "done", f"status {st.status}"


def test_limit_cap_keeps_most_recent() -> None:
    seen: list[str] = []
    patches = [
        (ni, "enumerate_native_sessions", lambda *a, **k: list(_SCRAMBLED)),
        (ni, "import_session", lambda sess: seen.append(sess.native_id)),
        (ni, "already_imported_keys", lambda: set()),
    ]
    with _test_home.scoped_patches(patches):
        st = ni.JobStatus()
        ni._run_import(st, None, 2)
    assert seen == ["newest", "second"], f"limited order {seen}"
    assert st.imported == 2, f"limited imported {st.imported} != 2"
