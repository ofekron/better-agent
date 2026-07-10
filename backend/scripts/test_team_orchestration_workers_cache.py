from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import threading
import time
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-team-workers-cache-")
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import session_store  # noqa: E402
import team_orchestration_read as read  # noqa: E402
import team_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from stores import worker_store  # noqa: E402


CWD = "/cache/project"


def _seed(count: int = 459) -> list[str]:
    workers = []
    sids = []
    for index in range(count):
        session = session_manager.create(
            name=f"worker {index:03d}",
            cwd=CWD,
            orchestration_mode="native",
            source="cli",
        )
        sid = session["id"]
        sids.append(sid)
        workers.append({
            "agent_session_id": sid,
            "name": f"worker {index:03d}",
            "role_key": f"role-{index}",
            "cwd": CWD,
            "orchestration_mode": "native",
            "agent_sid": f"agent-{index}",
            "node_id": "primary",
            "created_at": f"2026-01-01T00:00:{index:03d}",
            "last_active": f"2026-01-01T00:00:{index:03d}",
            "delegation_count": index,
            "token_usage": {},
            "tags": ["pool"] if index % 2 else [],
        })
    registry = worker_store._empty()
    registry["workers"] = workers
    with worker_store._lock_for():
        worker_store._write(CWD, registry, refresh_worker_summaries=False)
    session_manager.flush_pending_persists()
    read._PROJECTION_OWNER.reset_for_tests()
    return sids


def _payload(shape: str = "auth-a") -> dict:
    return json.loads(read.workers_response_bytes(CWD, shape))


def test_concurrent_cold_singleflight_and_warm_latency() -> list[str]:
    sids = _seed()
    original = read._build_workers_projection
    count_lock = threading.Lock()
    builds = 0

    def counted(cwd: str):
        nonlocal builds
        with count_lock:
            builds += 1
        return original(cwd)

    read._build_workers_projection = counted
    try:
        started = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
            payloads = list(pool.map(lambda _index: read.workers_response_bytes(CWD, "auth-a"), range(48)))
        cold_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        warm = [read.workers_response_bytes(CWD, "auth-a") for _ in range(20)]
        warm_ms = (time.perf_counter() - started) * 1000 / len(warm)
    finally:
        read._build_workers_projection = original
    assert builds == 1, builds
    assert len(set(payloads)) == 1 and payloads[0] == warm[0]
    assert len(json.loads(payloads[0])["workers"]) == 459
    assert warm_ms * 2 < cold_ms, (cold_ms, warm_ms)
    return sids


def test_each_dependency_invalidates(sids: list[str]) -> None:
    initial = _payload()
    cold = read._PROJECTION_OWNER.stats_for_tests()[1]

    worker_store.touch_worker(CWD, sids[0])
    touched = _payload()
    assert touched["workers"][0]["delegation_count"] != initial["workers"][0]["delegation_count"]
    assert read._PROJECTION_OWNER.stats_for_tests()[1] == cold + 1
    cold += 1

    worker_store.enqueue_pool_task("pool", {"id": "cache-pool-task"})
    pooled = _payload()
    assert next(pool for pool in pooled["pools"] if pool["tag"] == "pool")["queued_count"] == 1
    assert read._PROJECTION_OWNER.stats_for_tests()[1] == cold + 1
    cold += 1

    worker_store.set_fork(CWD, "revision-caller", sids[0], "revision-fork")
    _payload()
    assert read._PROJECTION_OWNER.stats_for_tests()[1] == cold + 1
    cold += 1

    session_manager.rename(sids[0], "renamed worker")
    session_manager.flush_pending_persists()
    renamed = _payload()
    assert any(worker["display_name"] == "renamed worker" for worker in renamed["workers"])
    assert read._PROJECTION_OWNER.stats_for_tests()[1] == cold + 1
    cold += 1

    team = team_store.create(root_session_id=sids[0], team_id="cache-team")
    team_store.upsert_member(
        team["id"],
        member_id="worker",
        member_type="worker",
        agent_session_id=sids[0],
        role="cache-role",
    )
    teamed = _payload()
    assert teamed["teams"] and teamed["teams"][0]["workers"][0]["team_role"] == "cache-role"
    assert read._PROJECTION_OWNER.stats_for_tests()[1] == cold + 1
    cold += 1

def test_mutation_during_build_never_publishes_stale(sids: list[str]) -> None:
    read._PROJECTION_OWNER.reset_for_tests()
    original = read._build_workers_projection
    built = threading.Event()
    release = threading.Event()
    attempts = 0

    def blocked(cwd: str):
        nonlocal attempts
        result = original(cwd)
        attempts += 1
        if attempts == 1:
            built.set()
            assert release.wait(timeout=10)
        return result

    read._build_workers_projection = blocked
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(_payload)
            assert built.wait(timeout=10)
            session_manager.rename(sids[1], "mutated during build")
            session_manager.flush_pending_persists()
            release.set()
            result = future.result(timeout=20)
    finally:
        read._build_workers_projection = original
    assert attempts == 2, attempts
    assert any(worker["display_name"] == "mutated during build" for worker in result["workers"])
    assert read._PROJECTION_OWNER.stats_for_tests()[1] == 1


def test_native_json_dependency_invalidates(sids: list[str]) -> None:
    from orchs import jsonl_helpers

    sid = sids[4]
    native_sid = "cache-native-json"
    session_manager.set_agent_sid(sid, "native", native_sid)
    session_manager.flush_pending_persists()
    worker_store.upsert_worker(CWD, sid, "native", native_sid)
    worker_store.set_fork(CWD, "cache-caller", sid, "cache-fork")
    with worker_store._lock_for():
        registry = worker_store._read()
        record = registry["forks"]["cache-caller"][sid]
        record["parent_agent_sid"] = native_sid
        record["parent_line_count_at_fork"] = 1
        worker_store._write(CWD, registry, refresh_worker_summaries=False)
    native_path = Path(os.environ["BETTER_AGENT_HOME"]) / "cache-native.jsonl"
    native_path.write_text('{"line":1}\n', encoding="utf-8")
    original_compute = jsonl_helpers.compute_jsonl_path
    jsonl_helpers.compute_jsonl_path = lambda _cwd, _sid: native_path
    read._PROJECTION_OWNER.reset_for_tests()
    try:
        first = _payload()
        first_worker = next(worker for worker in first["workers"] if worker["agent_session_id"] == sid)
        assert first_worker["diverged"] is False
        unrelated_path = native_path.with_name("unrelated-native.jsonl")
        unrelated_path.write_text('{"line":1}\n', encoding="utf-8")
        jsonl_helpers.notify_jsonl_appended(unrelated_path)
        assert _payload() == first
        assert read._PROJECTION_OWNER.stats_for_tests()[1] == 1
        native_path.write_text('{"line":1}\n{"line":2}\n', encoding="utf-8")
        jsonl_helpers.notify_jsonl_appended(native_path)
        second = _payload()
        second_worker = next(worker for worker in second["workers"] if worker["agent_session_id"] == sid)
        assert second_worker["diverged"] is True
        assert read._PROJECTION_OWNER.stats_for_tests()[1] == 2
    finally:
        jsonl_helpers.compute_jsonl_path = original_compute


def test_delete_reorder_and_auth_isolation(sids: list[str]) -> None:
    before = _payload()
    assert worker_store.remove_worker(CWD, sids[2])
    after_delete = _payload()
    assert len(after_delete["workers"]) == len(before["workers"]) - 1
    worker_store.touch_worker(CWD, sids[3])
    reordered = _payload()
    assert reordered["workers"][0]["agent_session_id"] == sids[3]

    read._PROJECTION_OWNER.reset_for_tests()
    a = read.workers_response_bytes(CWD, "auth-a")
    b = read.workers_response_bytes(CWD, "auth-b")
    assert a == b
    assert read._PROJECTION_OWNER.stats_for_tests()[1] == 1


def test_cache_cardinality_and_byte_bounds() -> None:
    read._PROJECTION_OWNER.reset_for_tests()
    for index in range(read._PROJECTION_OWNER._MAX_ENTRIES + 9):
        read.workers_response_bytes(f"{CWD}/{index}")
    _revision, _cold, entries, byte_count = read._PROJECTION_OWNER.stats_for_tests()
    assert entries <= read._PROJECTION_OWNER._MAX_ENTRIES
    assert byte_count <= read._PROJECTION_OWNER._MAX_BYTES


def main() -> int:
    sids = test_concurrent_cold_singleflight_and_warm_latency()
    test_each_dependency_invalidates(sids)
    test_mutation_during_build_never_publishes_stale(sids)
    test_native_json_dependency_invalidates(sids)
    test_delete_reorder_and_auth_isolation(sids)
    test_cache_cardinality_and_byte_bounds()
    print("PASS team orchestration workers cache")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
