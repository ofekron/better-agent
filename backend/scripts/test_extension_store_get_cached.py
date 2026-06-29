"""Regression: extension_store.get_extension must be fingerprint-cached so the
per-request internal-extension auth path doesn't take the cross-process
fcntl.flock + disk read on the event loop.

The faulthandler watchdog ranked `extension_store._store_lock` the #3
event-loop blocker (acquire-wait via contextlib.__enter__). `get_extension`
(called directly AND via `is_extension_active`) ran `_load()` -> `_store_lock()`
on every call with no cache, so each guarded request took the lock twice on
the loop.

This pins the fix: a warm read takes the lock zero times, a store write
(fingerprint change) invalidates, and `_clear_projection_cache()` drops it for
a same-fingerprint forced refresh. Also asserts callers get an isolated copy.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import _test_home
_test_home.isolate("bc-test-ext-get-cached-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import extension_store  # noqa: E402


def _seed_store(extensions: dict) -> None:
    path = extension_store._store_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": extension_store.STORE_SCHEMA_VERSION,
                "extensions": extensions,
                "deleted_extensions": {},
            }
        ),
        encoding="utf-8",
    )
    extension_store._clear_projection_cache()


def _record(ext_id: str, enabled: bool = True) -> dict:
    return {
        "manifest": {"id": ext_id, "permissions": {}},
        "enabled": enabled,
        "entitlement": {},
    }


class _LockCounter:
    """Wrap _store_lock to count acquisitions."""

    def __init__(self) -> None:
        self.count = 0
        self._real = extension_store._store_lock

    def __enter__(self):
        extension_store._store_lock = self._counting
        return self

    def __exit__(self, *a):
        extension_store._store_lock = self._real

    def _counting(self):
        self.count += 1
        return self._real()


def test_get_extension_is_fingerprint_cached() -> None:
    _seed_store({"a": _record("a")})

    with _LockCounter() as lc:
        first = extension_store.get_extension("a")
        assert first is not None and first["manifest"]["id"] == "a"
        assert lc.count == 1, f"cold read takes the lock once, got {lc.count}"

        # Warm reads: no lock at all.
        for _ in range(5):
            again = extension_store.get_extension("a")
            assert again is not None and again["manifest"]["id"] == "a"
        assert lc.count == 1, f"warm reads must not take the lock, got {lc.count}"

        # Missing id is also cached (returns None without re-locking).
        assert extension_store.get_extension("missing") is None
        assert extension_store.get_extension("missing") is None
        assert lc.count == 2, f"missing-id cold read locks once then caches, got {lc.count}"


def test_returned_record_is_isolated_copy() -> None:
    _seed_store({"a": _record("a")})
    one = extension_store.get_extension("a")
    one["manifest"]["id"] = "MUTATED"
    two = extension_store.get_extension("a")
    assert two["manifest"]["id"] == "a", "cache must hand out an isolated deepcopy"


def test_store_write_invalidates_cache() -> None:
    _seed_store({"a": _record("a", enabled=True)})
    assert extension_store.get_extension("a")["enabled"] is True

    import time
    time.sleep(0.01)  # ensure mtime_ns advances
    _seed_store({"a": _record("a", enabled=False)})  # rewrites file -> new fingerprint
    assert extension_store.get_extension("a")["enabled"] is False, (
        "a store write must invalidate the get_extension cache via fingerprint"
    )


def test_clear_projection_cache_drops_get_extension_cache() -> None:
    _seed_store({"a": _record("a")})
    extension_store.get_extension("a")
    assert extension_store._GET_EXTENSION_CACHE, "cache should be populated"
    extension_store._clear_projection_cache()
    assert not extension_store._GET_EXTENSION_CACHE, (
        "_clear_projection_cache must drop the get_extension cache too"
    )


if __name__ == "__main__":
    test_get_extension_is_fingerprint_cached()
    test_returned_record_is_isolated_copy()
    test_store_write_invalidates_cache()
    test_clear_projection_cache_drops_get_extension_cache()
    print("PASS extension_store get_extension cached")
