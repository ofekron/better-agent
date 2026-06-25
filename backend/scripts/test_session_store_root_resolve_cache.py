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
    session_store._index_refresh_attempt_until.clear()  # type: ignore[attr-defined]
    session_store._index_refresh_global_attempt_until = 0.0  # type: ignore[attr-defined]
    session_store._index_loaded = False  # type: ignore[attr-defined]
    session_store._index_fingerprint = None  # type: ignore[attr-defined]
    session_manager._node_root_id.clear()  # type: ignore[attr-defined]
    session_manager._node_root_missing_until.clear()  # type: ignore[attr-defined]


def test_unknown_sid_resolution_is_negative_cached_until_dir_changes() -> None:
    _reset_index()
    refresh_calls = 0
    original_refresh = session_store._refresh_index  # type: ignore[attr-defined]

    def counted_refresh(*args, **kwargs) -> tuple[int, int, int]:
        nonlocal refresh_calls
        refresh_calls += 1
        return original_refresh(*args, **kwargs)

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
        assert refresh_calls == 1
    finally:
        session_store._refresh_index = original_refresh  # type: ignore[attr-defined]


def test_session_manager_unknown_sid_resolution_is_negative_cached() -> None:
    _reset_index()
    calls = 0
    original_resolve = session_store._resolve_root_id  # type: ignore[attr-defined]

    def counted_resolve(sid: str) -> str | None:
        nonlocal calls
        calls += 1
        return original_resolve(sid)

    session_store._resolve_root_id = counted_resolve  # type: ignore[attr-defined]
    try:
        assert session_manager._root_id_for("missing-sid") is None  # type: ignore[attr-defined]
        assert session_manager._root_id_for("missing-sid") is None  # type: ignore[attr-defined]
        assert calls == 1
    finally:
        session_store._resolve_root_id = original_resolve  # type: ignore[attr-defined]


def test_session_store_root_sid_skips_index_load() -> None:
    _reset_index()
    created = session_manager.create(
        name="root-fast",
        cwd="/tmp/project",
        orchestration_mode="native",
    )
    sid = created["id"]
    original_ensure = session_store._ensure_index  # type: ignore[attr-defined]

    def fail_ensure() -> None:
        raise AssertionError("root sid should not load the fork index")

    session_store._ensure_index = fail_ensure  # type: ignore[attr-defined]
    try:
        assert session_store._resolve_root_id(sid) == sid  # type: ignore[attr-defined]
    finally:
        session_store._ensure_index = original_ensure  # type: ignore[attr-defined]


def test_loaded_fork_mapping_wins_over_stray_root_file() -> None:
    _reset_index()
    root = session_manager.create(
        name="root-with-fork",
        cwd="/tmp/project",
        orchestration_mode="native",
    )
    child_id = "child-stray"
    with session_store._index_lock:  # type: ignore[attr-defined]
        session_store._index_loaded = True  # type: ignore[attr-defined]
        session_store._fork_index[child_id] = root["id"]  # type: ignore[attr-defined]
        session_store._root_forks[root["id"]] = {child_id}  # type: ignore[attr-defined]
        session_store._root_index_signatures[root["id"]] = (  # type: ignore[attr-defined]
            session_store.session_file_fingerprint(root["id"])
        )
    (session_store._sessions_dir() / f"{child_id}.json").write_text(  # type: ignore[attr-defined]
        '{"id":"stray-child-file"}',
        encoding="utf-8",
    )

    assert session_store._resolve_root_id(child_id) == root["id"]  # type: ignore[attr-defined]
    session_manager._node_root_id.clear()  # type: ignore[attr-defined]
    assert session_manager._root_id_for(child_id) == root["id"]  # type: ignore[attr-defined]


if __name__ == "__main__":
    test_unknown_sid_resolution_is_negative_cached_until_dir_changes()
    test_session_manager_unknown_sid_resolution_is_negative_cached()
    test_session_store_root_sid_skips_index_load()
    test_loaded_fork_mapping_wins_over_stray_root_file()
    print("PASS root resolve negative cache")
