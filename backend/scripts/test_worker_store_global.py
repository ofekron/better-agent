import os
import concurrent.futures
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-worker-store-global-")

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from paths import ba_home  # noqa: E402
import session_store  # noqa: E402
from stores import session_fork_store, worker_store  # noqa: E402


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_global_worker_lookup_ignores_query_cwd() -> None:
    rec = worker_store.upsert_worker(
        "/repo/a",
        "worker-a",
        "native",
        "agent-a",
        node_id="primary",
    )

    check(rec["cwd"] == "/repo/a", "worker stores creation cwd")
    check(
        worker_store.get_worker("/repo/b", "worker-a") is not None,
        "get_worker finds registered worker from any cwd",
    )
    check(
        [w["agent_session_id"] for w in worker_store.list_workers("")] == ["worker-a"],
        "blank cwd lists global workers",
    )
    check(
        [w["agent_session_id"] for w in worker_store.list_workers("/repo/a")] == ["worker-a"],
        "matching cwd filter includes worker",
    )
    check(
        worker_store.list_workers("/repo/b") == [],
        "non-matching cwd filter excludes worker",
    )
    check(
        (ba_home() / "workers" / "global.json").exists(),
        "worker store writes global file",
    )


def test_global_forks_ignore_cwd() -> None:
    worker_store.set_fork("/repo/a", "caller-a", "worker-a", "fork-a")

    rec = worker_store.get_fork_record("/repo/b", "caller-a", "worker-a")

    check(rec is not None, "fork record is global")
    check(rec["fork_agent_session_id"] == "fork-a", "fork id round-trips")
    check(
        worker_store.clear_fork("/repo/b", "caller-a", "worker-a") is True,
        "clear_fork works from any cwd",
    )
    check(
        worker_store.get_fork_record("/repo/a", "caller-a", "worker-a") is None,
        "global fork record was removed",
    )


def test_session_fork_store_uses_neutral_session_keyword() -> None:
    session_fork_store.set_fork(
        cwd="/repo/a",
        caller_agent_session_id="caller-session",
        session_agent_session_id="target-session",
        fork_agent_session_id="fork-session",
    )

    rec = session_fork_store.get_fork_record(
        "/repo/b", "caller-session", "target-session",
    )

    check(rec is not None, "session fork record is stored")
    check(
        rec["fork_agent_session_id"] == "fork-session",
        "session fork id round-trips through neutral API",
    )


def test_remove_worker_is_global() -> None:
    worker_store.upsert_worker("/repo/a", "worker-remove", "native", "agent-r")
    worker_store.set_fork("/repo/a", "caller-r", "worker-remove", "fork-r")

    check(
        worker_store.remove_worker("/repo/b", "worker-remove") is True,
        "remove_worker removes globally from any cwd",
    )
    check(
        worker_store.get_worker("/repo/a", "worker-remove") is None,
        "worker was removed globally",
    )
    check(
        worker_store.get_fork_record("/repo/a", "caller-r", "worker-remove") is None,
        "worker removal clears global fork",
    )


def test_worker_count_neutral_writes_do_not_refresh_session_summaries() -> None:
    calls = 0
    original = session_store._refresh_all_worker_summaries

    def record_refresh() -> None:
        nonlocal calls
        calls += 1

    session_store._refresh_all_worker_summaries = record_refresh
    try:
        worker_store.upsert_worker("/repo/a", "worker-refresh", "native", "agent-1")
        check(calls == 1, "adding a worker refreshes summary worker_count")

        worker_store.upsert_worker("/repo/a", "worker-refresh", "native", "agent-2")
        worker_store.touch_worker("/repo/a", "worker-refresh")
        worker_store.enqueue_pool_task("review", {"id": "task-1"})
        worker_store.pop_pool_task("review", "task-1")
        worker_store.set_fork("/repo/a", "caller-refresh", "worker-refresh", "fork-refresh")
        worker_store.touch_fork("/repo/a", "caller-refresh", "worker-refresh")
        worker_store.clear_fork("/repo/a", "caller-refresh", "worker-refresh")
        check(calls == 1, "count-neutral worker writes do not refresh summaries")

        worker_store.remove_worker("/repo/a", "worker-refresh")
        check(calls == 2, "removing a worker refreshes summary worker_count")
    finally:
        session_store._refresh_all_worker_summaries = original


def test_worker_count_hot_cache_skips_fingerprint() -> None:
    worker_store.upsert_worker("/repo/a", "worker-hot-count", "native", "agent-hot")
    calls = 0
    original_fingerprint = worker_store._file_fingerprint

    def counted_fingerprint():
        nonlocal calls
        calls += 1
        return original_fingerprint()

    worker_store._file_fingerprint = counted_fingerprint
    try:
        first = worker_store.worker_count("")
        second = worker_store.worker_count("")
        check(first == second, "hot worker count changed")
        check(calls == 1, f"hot worker count re-fingerprinted registry: {calls}")
    finally:
        worker_store._file_fingerprint = original_fingerprint
        worker_store.remove_worker("/repo/a", "worker-hot-count")


def test_worker_registry_path_is_cached() -> None:
    worker_store._workers_dir_cache = None
    calls = 0
    original_ba_home = worker_store.ba_home

    def counted_ba_home():
        nonlocal calls
        calls += 1
        return original_ba_home()

    worker_store.ba_home = counted_ba_home
    try:
        first = worker_store._path()
        second = worker_store._path()
        check(first == second, "worker registry path changed")
        check(calls == 1, f"worker registry path resolved repeatedly: {calls}")
    finally:
        worker_store.ba_home = original_ba_home
        worker_store._workers_dir_cache = None


def test_worker_registry_read_cache_is_fingerprinted_and_isolated() -> None:
    worker_store.upsert_worker("/repo/a", "worker-read-cache", "native", "agent-read-cache")
    original_read_text = worker_store.Path.read_text
    calls = 0

    def counted_read_text(path, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original_read_text(path, *args, **kwargs)

    worker_store.Path.read_text = counted_read_text
    try:
        first = worker_store._read()
        first["workers"].append({"agent_session_id": "mutated"})
        second = worker_store._read()
        check(calls == 0, f"hot registry read touched disk: {calls}")
        check(
            all(w.get("agent_session_id") != "mutated" for w in second.get("workers", [])),
            "registry cache leaked caller mutation",
        )
    finally:
        worker_store.Path.read_text = original_read_text
        worker_store.remove_worker("/repo/a", "worker-read-cache")


def test_activity_journal_scales_and_replays_exactly() -> None:
    workers = [{
        "agent_session_id": f"scale-{index}",
        "cwd": "/repo/scale",
        "orchestration_mode": "native",
        "agent_sid": f"agent-{index}",
        "last_active": "2020-01-01T00:00:00",
        "delegation_count": 0,
        "token_usage": {},
    } for index in range(2000)]
    worker_store.write_json(worker_store._path(), {
        "version": worker_store.SCHEMA_VERSION,
        "workers": workers,
        "forks": {},
        "pool_queues": {},
        "pool_failed_tasks": {},
    })
    worker_store._registry_cache = None
    worker_store._registry_cache_signature = None
    worker_store._activity_loaded = False
    worker_store._activity_by_worker = {}
    original_compact_every = worker_store._ACTIVITY_COMPACT_EVERY
    worker_store._ACTIVITY_COMPACT_EVERY = 10
    latencies: list[float] = []

    def touch(_index: int) -> None:
        started = time.perf_counter()
        commit = worker_store.touch_worker("", "scale-0", {"input_tokens": 1})
        check(commit is not None, "registered worker touch disappeared")
        latencies.append((time.perf_counter() - started) * 1000)

    registry_mtime = worker_store._path().stat().st_mtime_ns
    worker_store._read()
    class CountingMembership(set):
        checks = 0

        def __contains__(self, value):
            self.checks += 1
            return super().__contains__(value)

    membership = CountingMembership(worker_store._registry_worker_ids)
    worker_store._registry_worker_ids = membership
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(touch, range(50)))
    check(membership.checks == 50, f"touch membership work was not constant: {membership.checks}")
    deadline = time.monotonic() + 10
    while worker_store._activity_compacting and time.monotonic() < deadline:
        time.sleep(0.01)
    check(not worker_store._activity_compacting, "activity compactor did not finish")
    check(worker_store._activity_checkpoint_path().exists(), "activity checkpoint missing")
    check(worker_store._path().stat().st_mtime_ns == registry_mtime, "touch rewrote structural registry")
    p95 = sorted(latencies)[int(len(latencies) * 0.95) - 1]
    check(p95 < 250, f"activity touch p95 too slow: {p95:.1f}ms")

    worker_store._registry_cache = None
    worker_store._registry_cache_signature = None
    worker_store._activity_loaded = False
    worker_store._activity_by_worker = {}
    replayed = worker_store.get_worker("", "scale-0")
    check(replayed is not None, "worker missing after activity replay")
    check(replayed["delegation_count"] == 50, "delegation count replay lost updates")
    check(replayed["token_usage"]["input_tokens"] == 50, "token replay lost updates")
    checkpoint = worker_store._activity_checkpoint_path()
    valid_checkpoint = checkpoint.read_bytes()
    checkpoint.write_text("{}", encoding="utf-8")
    worker_store._activity_loaded = False
    try:
        worker_store.activity_authority()
    except ValueError:
        pass
    else:
        raise AssertionError("corrupt activity checkpoint did not fail closed")
    checkpoint.write_bytes(valid_checkpoint)
    worker_store._activity_loaded = False
    worker_store.activity_authority()

    original_write_json = worker_store.write_json
    def fail_write(*_args, **_kwargs):
        raise OSError("injected")

    worker_store.write_json = fail_write
    try:
        try:
            worker_store.remove_worker("", "scale-0")
        except OSError:
            pass
        else:
            raise AssertionError("injected structural write failure was swallowed")
    finally:
        worker_store.write_json = original_write_json
    check(worker_store.touch_worker("", "scale-0") is not None, "failed removal tombstoned activity")
    check(worker_store.remove_worker("", "scale-0"), "worker removal failed")
    check(worker_store.touch_worker("", "scale-0") is None, "late touch resurrected removed worker")
    worker_store.upsert_worker("/repo/scale", "scale-0", "native", "agent-readded")
    readded = worker_store.touch_worker("", "scale-0", {"input_tokens": 2})
    check(readded is not None and readded.worker["delegation_count"] == 1, "re-add inherited tombstoned activity")
    worker_store._registry_cache = None
    worker_store._registry_cache_signature = None
    worker_store._activity_loaded = False
    worker_store._activity_by_worker = {}
    restarted = worker_store.get_worker("", "scale-0")
    check(restarted is not None and restarted["delegation_count"] == 1, "re-add restart replay diverged")

    original_boundary = worker_store._activity_compaction_boundary
    for boundary in ("checkpoint_committed", "journal_replaced"):
        def inject(stage, expected=boundary):
            if stage == expected:
                raise OSError(f"injected {expected}")

        worker_store._activity_compaction_boundary = inject
        worker_store._compact_activity()
        worker_store._activity_loaded = False
        worker_store._activity_by_worker = {}
        replayed_after_crash = worker_store.get_worker("", "scale-0")
        check(
            replayed_after_crash is not None and replayed_after_crash["delegation_count"] == 1,
            f"compaction boundary {boundary} lost committed suffix",
        )
    worker_store._activity_compaction_boundary = original_boundary
    worker_store._ACTIVITY_COMPACT_EVERY = original_compact_every


def main() -> int:
    try:
        test_global_worker_lookup_ignores_query_cwd()
        test_global_forks_ignore_cwd()
        test_session_fork_store_uses_neutral_session_keyword()
        test_remove_worker_is_global()
        test_worker_count_neutral_writes_do_not_refresh_session_summaries()
        test_worker_count_hot_cache_skips_fingerprint()
        test_worker_registry_read_cache_is_fingerprinted_and_isolated()
        test_activity_journal_scales_and_replays_exactly()
        print("ALL PASS")
        return 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
