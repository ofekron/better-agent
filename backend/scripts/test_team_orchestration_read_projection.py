from __future__ import annotations

import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-team-orch-read-")

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from session_manager import manager as session_manager  # noqa: E402
from stores import worker_store  # noqa: E402
import team_orchestration_read  # noqa: E402


def test_worker_projection_uses_summary_fields_before_full_session_read() -> None:
    worker = session_manager.create(
        name="summary worker",
        cwd="/repo/worker",
        orchestration_mode="native",
        source="cli",
    )
    worker_store.upsert_worker(
        "/repo/worker",
        worker["id"],
        "native",
        "agent-summary-worker",
    )

    original = team_orchestration_read.session_manager.get_fields_many

    def fail_full_read(*_args, **_kwargs):
        raise AssertionError("worker projection loaded full session fields")

    team_orchestration_read.session_manager.get_fields_many = fail_full_read
    try:
        projected = team_orchestration_read.list_workers_for_cwd("/repo/worker")
    finally:
        team_orchestration_read.session_manager.get_fields_many = original

    assert projected["workers"][0]["agent_session_id"] == worker["id"]
    assert projected["workers"][0]["display_name"] == "summary worker"


def test_stale_worker_sid_does_not_trigger_full_session_read() -> None:
    worker_store.upsert_worker(
        "/repo/stale",
        "missing-worker-session",
        "native",
        "agent-stale-worker",
    )

    original = team_orchestration_read.session_manager.get_fields_many

    def fail_full_read(*_args, **_kwargs):
        raise AssertionError("stale worker projection loaded full session fields")

    team_orchestration_read.session_manager.get_fields_many = fail_full_read
    try:
        projected = team_orchestration_read.list_workers_for_cwd("/repo/stale")
    finally:
        team_orchestration_read.session_manager.get_fields_many = original

    assert all(worker["agent_session_id"] != "missing-worker-session" for worker in projected["workers"])


def _seed_worker(
    *,
    cwd: str,
    name: str,
    tags: list[str] | None = None,
    native_sid: str | None = None,
) -> str:
    session = session_manager.create(
        name=name, cwd=cwd, orchestration_mode="native", source="cli"
    )
    sid = session["id"]
    if native_sid:
        session_manager.set_agent_sid(sid, "native", native_sid)
        session_manager.flush_pending_persists()
    worker_store.upsert_worker(cwd, sid, "native", native_sid, name=name, tags=tags)
    return sid


def _worker_by_sid(projected: dict, sid: str) -> dict:
    return next(worker for worker in projected["workers"] if worker["agent_session_id"] == sid)


def test_warm_hit_serves_cached_without_rebuild() -> None:
    team_orchestration_read._PROJECTION_OWNER.reset_for_tests()
    _seed_worker(cwd="/warm", name="warm worker")
    first = team_orchestration_read.workers_response_bytes("/warm")
    cold_after_first = team_orchestration_read._PROJECTION_OWNER.stats_for_tests()[1]
    second = team_orchestration_read.workers_response_bytes("/warm")
    cold_after_second = team_orchestration_read._PROJECTION_OWNER.stats_for_tests()[1]
    assert first == second
    assert cold_after_first == 1 and cold_after_second == 1


def test_dependency_change_rebuilds_and_accounts_prior_entry() -> None:
    team_orchestration_read._PROJECTION_OWNER.reset_for_tests()
    sid = _seed_worker(cwd="/prior", name="prior worker")
    team_orchestration_read.workers_response_bytes("/prior")
    session_manager.rename(sid, "prior renamed")
    session_manager.flush_pending_persists()
    team_orchestration_read.workers_response_bytes("/prior")
    _revision, cold_builds, entries, byte_count = team_orchestration_read._PROJECTION_OWNER.stats_for_tests()
    assert cold_builds == 2
    assert entries == 1
    assert 0 < byte_count <= team_orchestration_read._PROJECTION_OWNER._MAX_BYTES


def test_pool_projection_groups_by_tag_with_queue_counts() -> None:
    team_orchestration_read._PROJECTION_OWNER.reset_for_tests()
    _seed_worker(cwd="/pool", name="pool worker", tags=["alpha", "beta"])
    worker_store.enqueue_pool_task("alpha", {"id": "pool-task-a"})
    worker_store.enqueue_pool_task("alpha", {"id": "pool-task-b"})
    projected = team_orchestration_read.list_workers_for_cwd("/pool")
    by_tag = {pool["tag"]: pool for pool in projected["pools"]}
    assert "alpha" in by_tag and "beta" in by_tag
    assert by_tag["alpha"]["queued_count"] == 2
    assert by_tag["beta"]["queued_count"] == 0


def test_team_projection_binds_members_and_lists_available() -> None:
    import team_store

    team_orchestration_read._PROJECTION_OWNER.reset_for_tests()
    bound = _seed_worker(cwd="/team", name="bound worker")
    available = _seed_worker(cwd="/team", name="free worker")
    team = team_store.create(root_session_id=bound, team_id="proj-team")
    team_store.upsert_member(
        team["id"],
        member_id="bound",
        member_type="worker",
        agent_session_id=bound,
        role="lead",
    )
    team_store.upsert_member(
        team["id"],
        member_id="ghost",
        member_type="worker",
        agent_session_id="nonexistent-worker-sid",
        role="ghost",
    )
    projected = team_orchestration_read.list_workers_for_cwd("/team")
    team_row = next(team_row for team_row in projected["teams"] if team_row["id"] == team["id"])
    by_binding = {row["agent_session_id"]: row for row in team_row["workers"]}
    assert by_binding[bound]["team_binding"] == "bound"
    assert by_binding[bound]["team_role"] == "lead"
    assert by_binding[available]["team_binding"] == "available"
    assert by_binding[available]["team_role"] == ""
    assert "nonexistent-worker-sid" not in by_binding


def _install_fork_record(
    *, cwd: str, caller: str, worker_sid: str, parent_agent_sid: str, parent_line_count: int
) -> None:
    worker_store.set_fork(cwd, caller, worker_sid, f"{worker_sid}-fork")
    with worker_store._lock_for():
        registry = worker_store._read()
        record = registry["forks"][caller][worker_sid]
        record["parent_agent_sid"] = parent_agent_sid
        record["parent_line_count_at_fork"] = parent_line_count
        worker_store._write(cwd, registry, refresh_worker_summaries=False)


def test_fork_pair_parent_mismatch_marks_worker_diverged() -> None:
    team_orchestration_read._PROJECTION_OWNER.reset_for_tests()
    native_sid = "native-mismatch"
    sid = _seed_worker(cwd="/fork-mismatch", name="fork worker", native_sid=native_sid)
    _install_fork_record(
        cwd="/fork-mismatch",
        caller="caller-mismatch",
        worker_sid=sid,
        parent_agent_sid="different-parent",
        parent_line_count=0,
    )
    projected = team_orchestration_read.list_workers_for_cwd("/fork-mismatch")
    assert _worker_by_sid(projected, sid)["diverged"] is True


def test_native_line_growth_marks_worker_diverged() -> None:
    import os

    import paths
    from orchs import jsonl_helpers

    team_orchestration_read._PROJECTION_OWNER.reset_for_tests()
    native_sid = "native-growth"
    sid = _seed_worker(cwd="/fork-native", name="native worker", native_sid=native_sid)
    native_path = paths.ba_home() / "native-growth.jsonl"
    native_path.write_text('{"line":1}\n', encoding="utf-8")
    _install_fork_record(
        cwd="/fork-native",
        caller="caller-native",
        worker_sid=sid,
        parent_agent_sid=native_sid,
        parent_line_count=1,
    )
    original_compute = jsonl_helpers.compute_jsonl_path
    jsonl_helpers.compute_jsonl_path = lambda _cwd, _sid: native_path
    try:
        first = team_orchestration_read.list_workers_for_cwd("/fork-native")
        assert _worker_by_sid(first, sid)["diverged"] is False
        native_path.write_text('{"line":1}\n{"line":2}\n', encoding="utf-8")
        jsonl_helpers.notify_jsonl_appended(native_path)
        second = team_orchestration_read.list_workers_for_cwd("/fork-native")
        assert _worker_by_sid(second, sid)["diverged"] is True
    finally:
        jsonl_helpers.compute_jsonl_path = original_compute


def test_native_path_resolution_failures_keep_worker_undiverged() -> None:
    from orchs import jsonl_helpers

    native_sid = "native-resolve"
    base_kwargs = dict(cwd="/fork-resolve", name="resolve worker", native_sid=native_sid)

    original_compute = jsonl_helpers.compute_jsonl_path
    original_count = jsonl_helpers.count_jsonl_lines

    try:
        team_orchestration_read._PROJECTION_OWNER.reset_for_tests()
        sid = _seed_worker(**base_kwargs)
        _install_fork_record(
            cwd="/fork-resolve",
            caller="caller-resolve",
            worker_sid=sid,
            parent_agent_sid=native_sid,
            parent_line_count=0,
        )
        jsonl_helpers.compute_jsonl_path = lambda _cwd, _sid: None
        projected = team_orchestration_read.list_workers_for_cwd("/fork-resolve")
        assert _worker_by_sid(projected, sid)["diverged"] is False

        team_orchestration_read._PROJECTION_OWNER.reset_for_tests()
        import paths

        native_path = paths.ba_home() / "native-resolve.jsonl"
        native_path.write_text('{"line":1}\n', encoding="utf-8")
        jsonl_helpers.compute_jsonl_path = lambda _cwd, _sid: native_path

        def _raise(_path):
            raise OSError("unreadable")

        jsonl_helpers.count_jsonl_lines = _raise
        projected = team_orchestration_read.list_workers_for_cwd("/fork-resolve")
        assert _worker_by_sid(projected, sid)["diverged"] is False
    finally:
        jsonl_helpers.compute_jsonl_path = original_compute
        jsonl_helpers.count_jsonl_lines = original_count


def main() -> int:
    test_worker_projection_uses_summary_fields_before_full_session_read()
    test_stale_worker_sid_does_not_trigger_full_session_read()
    test_warm_hit_serves_cached_without_rebuild()
    test_dependency_change_rebuilds_and_accounts_prior_entry()
    test_pool_projection_groups_by_tag_with_queue_counts()
    test_team_projection_binds_members_and_lists_available()
    test_fork_pair_parent_mismatch_marks_worker_diverged()
    test_native_line_growth_marks_worker_diverged()
    test_native_path_resolution_failures_keep_worker_undiverged()
    print("PASS team orchestration read projection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
