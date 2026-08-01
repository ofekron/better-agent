"""Full unit coverage for delegation_status_store.py.

The store is a thin atomic-write projection over ba_home(): each delegation
writes one JSON file whose name is sanitized through `_safe_id`, reads merge
tolerating missing/corrupt/non-dict payloads. This file covers the sanitize,
merge, atomic-write, async wrapper, and every read failure branch.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402

_HOME = _test_home.isolate("ba-delegation-status-store-")

import delegation_status_store as store  # noqa: E402


def _status_dir() -> Path:
    # Live resolver (ba_home()) so writes and reads agree under co-collection.
    return store.status_path("x").parent


def test_safe_id_strips_unsafe_characters():
    # Path separators and dots are stripped so a delegation id can never escape
    # the delegate-status directory or reference another file.
    assert store._safe_id("../etc/passwd") == "etcpasswd"
    assert store._safe_id("a.b/c") == "abc"
    assert store._safe_id("ok-Id_1") == "ok-Id_1"


def test_status_path_under_delegate_status_dir():
    path = store.status_path("del-1")
    assert path.name == "del-1.json"
    assert path.parent.name == "delegate-status"


def test_read_status_missing_file_returns_none():
    assert store.read_status("absent") is None


def test_read_status_malformed_json_returns_none():
    bad = _status_dir()
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "bad.json").write_text("{not json", encoding="utf-8")
    assert store.read_status("bad") is None


def test_read_status_non_dict_payload_returns_none():
    bad = _status_dir()
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "list.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert store.read_status("list") is None


def test_write_status_creates_file_and_reads_back():
    store.write_status("d1", state="running", count=3)
    data = store.read_status("d1")
    assert data == {"state": "running", "count": 3}


def test_write_status_merges_into_existing_fields():
    store.write_status("d2", state="running")
    store.write_status("d2", count=5, extra="x")
    data = store.read_status("d2")
    assert data == {"state": "running", "count": 5, "extra": "x"}


def test_write_status_creates_parent_directory():
    # A fresh delegation id has no parent dir; write must mkdir it.
    store.write_status("d3", state="done")
    assert store.status_path("d3").is_file()


def test_write_status_async_wraps_sync_write():
    asyncio.run(store.write_status_async("d4", state="async"))
    assert store.read_status("d4") == {"state": "async"}
