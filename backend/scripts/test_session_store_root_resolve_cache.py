from __future__ import annotations

import os
import sys
from pathlib import Path

import _test_home

_test_home.isolate("bc-test-root-resolve-cache-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import session_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def _reset_index() -> None:
    session_store._fork_index.clear()  # type: ignore[attr-defined]
    session_store._root_forks.clear()  # type: ignore[attr-defined]
    session_store._root_index_signatures.clear()  # type: ignore[attr-defined]
    session_store._negative_root_resolve_cache.clear()  # type: ignore[attr-defined]
    session_store._index_loaded = False  # type: ignore[attr-defined]
    session_store._index_fingerprint = None  # type: ignore[attr-defined]


def test_unknown_sid_resolution_is_negative_cached_until_dir_changes() -> None:
    _reset_index()
    refresh_calls = 0
    original_refresh = session_store._refresh_index  # type: ignore[attr-defined]

    def counted_refresh() -> None:
        nonlocal refresh_calls
        refresh_calls += 1
        original_refresh()

    session_store._refresh_index = counted_refresh  # type: ignore[attr-defined]
    try:
        assert session_store._resolve_root_id("missing-sid") is None  # type: ignore[attr-defined]
        assert session_store._resolve_root_id("missing-sid") is None  # type: ignore[attr-defined]
        assert refresh_calls == 1

        created = session_manager.create(
            name="root",
            cwd="/tmp/project",
            orchestration_mode="native",
        )
        assert created["id"]
        assert session_store._resolve_root_id("missing-sid") is None  # type: ignore[attr-defined]
        assert refresh_calls == 2
    finally:
        session_store._refresh_index = original_refresh  # type: ignore[attr-defined]


if __name__ == "__main__":
    test_unknown_sid_resolution_is_negative_cached_until_dir_changes()
    print("PASS root resolve negative cache")
