from __future__ import annotations

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
    session_store._index_refresh_attempt_until.clear()  # type: ignore[attr-defined]
    session_store._index_refresh_global_attempt_until = 0.0  # type: ignore[attr-defined]
    session_store._index_loaded = False  # type: ignore[attr-defined]
    session_store._index_fingerprint = None  # type: ignore[attr-defined]
    session_manager._node_root_id.clear()  # type: ignore[attr-defined]
    session_manager._node_root_missing_until.clear()  # type: ignore[attr-defined]


def test_unknown_sid_resolution_never_rescans_warm_index_on_request_path() -> None:
    _reset_index()
    session_store._ensure_index()  # type: ignore[attr-defined]
    calls = {"fingerprint": 0, "build": 0, "refresh": 0}
    original_fingerprint = session_store._dir_fingerprint_for  # type: ignore[attr-defined]
    original_build = session_store._build_index_snapshot  # type: ignore[attr-defined]
    original_refresh = session_store._refresh_index  # type: ignore[attr-defined]

    def counted_fingerprint(*args, **kwargs):
        calls["fingerprint"] += 1
        return original_fingerprint(*args, **kwargs)

    def counted_build(*args, **kwargs):
        calls["build"] += 1
        return original_build(*args, **kwargs)

    def counted_refresh(*args, **kwargs) -> tuple[int, int, int]:
        calls["refresh"] += 1
        return original_refresh(*args, **kwargs)

    session_store._dir_fingerprint_for = counted_fingerprint  # type: ignore[attr-defined]
    session_store._build_index_snapshot = counted_build  # type: ignore[attr-defined]
    session_store._refresh_index = counted_refresh  # type: ignore[attr-defined]
    try:
        assert session_store._resolve_root_id("missing-sid") is None  # type: ignore[attr-defined]
        assert session_store._resolve_root_id("missing-sid") is None  # type: ignore[attr-defined]
        assert calls == {"fingerprint": 0, "build": 0, "refresh": 0}

        created = session_manager.create(
            name="root",
            cwd="/tmp/project",
            orchestration_mode="native",
        )
        assert created["id"]
        before_lookup = dict(calls)
        assert session_store._resolve_root_id("missing-sid") is None  # type: ignore[attr-defined]
        assert calls == before_lookup
    finally:
        session_store._dir_fingerprint_for = original_fingerprint  # type: ignore[attr-defined]
        session_store._build_index_snapshot = original_build  # type: ignore[attr-defined]
        session_store._refresh_index = original_refresh  # type: ignore[attr-defined]


def test_session_manager_unknown_sid_resolution_is_negative_cached() -> None:
    _reset_index()
    calls = 0
    original_resolve = session_store._resolve_root_ids  # type: ignore[attr-defined]

    def counted_resolve(sids) -> dict[str, str]:
        nonlocal calls
        calls += 1
        return original_resolve(sids)

    session_store._resolve_root_ids = counted_resolve  # type: ignore[attr-defined]
    try:
        assert session_manager._root_id_for("missing-sid") is None  # type: ignore[attr-defined]
        assert session_manager._root_id_for("missing-sid") is None  # type: ignore[attr-defined]
        assert calls == 1
    finally:
        session_store._resolve_root_ids = original_resolve  # type: ignore[attr-defined]


def test_batch_unknown_sid_resolution_waits_for_one_observation() -> None:
    _reset_index()
    session_store._ensure_index()  # type: ignore[attr-defined]
    waits = 0
    original_wait = session_store._wait_root_change_observation  # type: ignore[attr-defined]
    original_binding = session_store._root_change_binding_for_read  # type: ignore[attr-defined]

    class _Owner:
        observation_generation = 7

        @staticmethod
        def wait_ready() -> None:
            return None

    class _Binding:
        owner = _Owner()

    def counted_wait(generation: int, timeout: float = 0.05) -> bool:
        nonlocal waits
        assert generation == 7
        assert timeout == 0.05
        waits += 1
        return False

    session_store._root_change_binding_for_read = lambda: _Binding()  # type: ignore[attr-defined]
    session_store._wait_root_change_observation = counted_wait  # type: ignore[attr-defined]
    try:
        resolved = session_store._resolve_root_ids(  # type: ignore[attr-defined]
            [f"missing-{index}" for index in range(200)]
        )
        assert resolved == {}
        assert waits == 1
    finally:
        session_store._wait_root_change_observation = original_wait  # type: ignore[attr-defined]
        session_store._root_change_binding_for_read = original_binding  # type: ignore[attr-defined]


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


def test_active_run_sid_is_never_negative_cached() -> None:
    """Regression: a sid with an in-flight turn must not be blacked out by
    the negative-cache TTL. Before the fix, a single transient
    eventual-consistency miss during an active turn poisoned
    `_node_root_missing_until[sid]` for `_NEGATIVE_NODE_ROOT_TTL_SECONDS`,
    and every subsequent lookup in that window (including ones for a sid
    that resolves successfully moments later) returned None — this is what
    produced `RuntimeError("cannot resolve root for session ...")` and the
    `session_manager.batch()` KeyError while a turn was actively running."""
    _reset_index()
    session_manager._active_run_gate = lambda sid: sid == "active-sid"  # type: ignore[attr-defined]
    try:
        # First miss while "active": must NOT poison the negative cache.
        assert session_manager._root_id_for("active-sid") is None  # type: ignore[attr-defined]
        assert "active-sid" not in session_manager._node_root_missing_until  # type: ignore[attr-defined]

        # A genuinely inactive sid still gets negative-cached as before.
        assert session_manager._root_id_for("inactive-sid") is None  # type: ignore[attr-defined]
        assert "inactive-sid" in session_manager._node_root_missing_until  # type: ignore[attr-defined]

        # Manually poison "active-sid" (simulating a stale entry written
        # before the run started) and confirm the fix self-heals it instead
        # of honoring the TTL blackout.
        session_manager._node_root_missing_until["active-sid"] = (  # type: ignore[attr-defined]
            __import__("time").monotonic() + 5.0
        )
        created = session_manager.create(
            name="active-root",
            cwd="/tmp/project",
            orchestration_mode="native",
        )
        session_manager._active_run_gate = lambda sid: sid == created["id"]  # type: ignore[attr-defined]
        session_manager._node_root_missing_until[created["id"]] = (  # type: ignore[attr-defined]
            __import__("time").monotonic() + 5.0
        )
        session_manager._node_root_id.pop(created["id"], None)  # type: ignore[attr-defined]
        assert session_manager._root_id_for(created["id"]) == created["id"]  # type: ignore[attr-defined]
    finally:
        session_manager._active_run_gate = None  # type: ignore[attr-defined]


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
    test_unknown_sid_resolution_never_rescans_warm_index_on_request_path()
    test_session_manager_unknown_sid_resolution_is_negative_cached()
    test_batch_unknown_sid_resolution_waits_for_one_observation()
    test_active_run_sid_is_never_negative_cached()
    test_session_store_root_sid_skips_index_load()
    test_loaded_fork_mapping_wins_over_stray_root_file()
    print("PASS root resolve owner projection")
