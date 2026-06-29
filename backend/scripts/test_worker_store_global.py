import os
import shutil
import sys
import tempfile
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


def main() -> int:
    try:
        test_global_worker_lookup_ignores_query_cwd()
        test_global_forks_ignore_cwd()
        test_session_fork_store_uses_neutral_session_keyword()
        test_remove_worker_is_global()
        test_worker_count_neutral_writes_do_not_refresh_session_summaries()
        test_worker_count_hot_cache_skips_fingerprint()
        print("ALL PASS")
        return 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
