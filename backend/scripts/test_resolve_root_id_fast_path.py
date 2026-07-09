from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-root-id-fast-path-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _reset_home() -> None:
    session_store._index_sidecar_write_queue.join()
    sessions_dir = Path(_TMP_HOME) / "sessions"
    if sessions_dir.exists():
        shutil.rmtree(sessions_dir)
    session_store._fork_index.clear()
    session_store._root_forks.clear()
    session_store._root_index_signatures.clear()
    session_store._index_refresh_attempt_until.clear()
    session_store._index_refresh_global_attempt_until = 0.0
    session_store._index_loaded = False
    session_store._index_fingerprint = None
    session_store._summary_index.clear()
    session_store._summary_index_loaded = False
    session_store._summary_index_version = 0
    session_store._summary_order_version = 0
    session_store._summary_sorted_id_cache = []
    session_store._summary_sorted_cache_version = -1


def _record(sid: str, forks: list[dict] | None = None) -> dict:
    return {
        "_schema_version": session_store.SCHEMA_VERSION,
        "id": sid,
        "name": sid,
        "model": "gpt-5.5",
        "cwd": "/tmp/root-fast-path",
        "orchestration_mode": "native",
        "kind": "user",
        "parent_session_id": None,
        "forks": forks or [],
        "messages": [],
        "next_seq": 0,
        "created_at": "2026-06-22T00:00:00+00:00",
        "updated_at": "2026-06-22T00:00:00+00:00",
        "source": "cli",
    }


def _write(record: dict) -> None:
    sessions_dir = Path(_TMP_HOME) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    with open(sessions_dir / f"{record['id']}.json", "w", encoding="utf-8") as f:
        json.dump(record, f)


def _write_summary(
    sid: str,
    fork_count: int,
    *,
    fork_ids: list[str] | None = None,
    stale: bool = False,
) -> None:
    sessions_dir = Path(_TMP_HOME) / "sessions"
    path = sessions_dir / f"{sid}.summary.json"
    path.write_text(
        json.dumps({
            "id": sid,
            "updated_at": "2026-06-22T00:00:00+00:00",
            "fork_count": fork_count,
            "fork_ids": fork_ids if fork_ids is not None else [],
            "last_seen_event_uid": None,
            "current_todos": [],
            "current_tasks": [],
        }),
        encoding="utf-8",
    )
    if stale:
        session_path = sessions_dir / f"{sid}.json"
        os.utime(path, (1, 1))
        os.utime(session_path, None)


def test_root_id_resolves_without_cold_scan() -> bool:
    _reset_home()
    _write(_record("target-root"))
    for i in range(20):
        _write(_record(f"other-{i}"))

    original_build = session_store._build_index_snapshot
    calls = 0

    def fail_build():
        nonlocal calls
        calls += 1
        raise AssertionError("_resolve_root_id scanned for a root sid")

    session_store._build_index_snapshot = fail_build
    try:
        resolved = session_store._resolve_root_id("target-root")
    finally:
        session_store._build_index_snapshot = original_build

    ok = resolved == "target-root" and calls == 0 and session_store._index_loaded is False
    print(f"{PASS if ok else FAIL} root sid resolves without cold fork-index scan")
    return ok


def test_fork_id_still_uses_index() -> bool:
    _reset_home()
    fork = {
        **_record("child-fork"),
        "parent_session_id": "target-root",
        "fork_point_seq": 0,
    }
    _write(_record("target-root", forks=[fork]))

    resolved = session_store._resolve_root_id("child-fork")
    ok = (
        resolved == "target-root"
        and session_store._index_loaded is True
        and session_store._fork_index.get("child-fork") == "target-root"
    )
    print(f"{PASS if ok else FAIL} fork sid still resolves through fork index")
    return ok


def test_concurrent_fork_misses_singleflight_cold_build() -> bool:
    _reset_home()
    fork = {
        **_record("child-fork"),
        "parent_session_id": "target-root",
        "fork_point_seq": 0,
    }
    _write(_record("target-root", forks=[fork]))

    original_build = session_store._build_index_snapshot
    calls = 0
    calls_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def tracking_build():
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.02)
        return original_build()

    def resolve() -> None:
        barrier.wait()
        if session_store._resolve_root_id("child-fork") != "target-root":
            raise AssertionError("fork did not resolve")

    session_store._build_index_snapshot = tracking_build
    try:
        threads = [threading.Thread(target=resolve) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        session_store._build_index_snapshot = original_build

    ok = calls == 1
    print(
        f"{PASS if ok else FAIL} concurrent fork misses singleflight cold build"
        f"{'' if ok else ' calls=' + repr(calls)}"
    )
    return ok


def test_fresh_zero_fork_summaries_skip_full_parse() -> bool:
    _reset_home()
    _write(_record("target-root"))
    _write_summary("target-root", 0)
    for i in range(200):
        sid = f"other-{i}"
        _write(_record(sid))
        _write_summary(sid, 0)

    original_loads = session_store.json.loads
    parsed_roots: list[str] = []

    def tracking_loads(raw: str, *args, **kwargs):
        data = original_loads(raw, *args, **kwargs)
        if isinstance(data, dict) and data.get("_schema_version") == session_store.SCHEMA_VERSION:
            parsed_roots.append(data.get("id"))
        return data

    session_store.json.loads = tracking_loads
    try:
        resolved = session_store._resolve_root_id("missing-fork")
    finally:
        session_store.json.loads = original_loads

    ok = resolved is None and parsed_roots == []
    print(
        f"{PASS if ok else FAIL} fresh zero-fork summaries skip full root parses"
        f"{'' if ok else ' parsed=' + repr(parsed_roots[:10])}"
    )
    return ok


def test_fresh_fork_summary_builds_index_without_root_parse() -> bool:
    _reset_home()
    fork = {
        **_record("child-fork"),
        "parent_session_id": "target-root",
        "fork_point_seq": 0,
    }
    _write(_record("target-root", forks=[fork]))
    _write_summary("target-root", 1, fork_ids=["child-fork"])

    original_loads = session_store.json.loads
    parsed_roots: list[str] = []

    def tracking_loads(raw: str, *args, **kwargs):
        data = original_loads(raw, *args, **kwargs)
        if isinstance(data, dict) and data.get("_schema_version") == session_store.SCHEMA_VERSION:
            parsed_roots.append(data.get("id"))
        return data

    session_store.json.loads = tracking_loads
    try:
        resolved = session_store._resolve_root_id("child-fork")
    finally:
        session_store.json.loads = original_loads

    ok = resolved == "target-root" and parsed_roots == []
    print(
        f"{PASS if ok else FAIL} fresh fork summary builds index without root parse"
        f"{'' if ok else ' parsed=' + repr(parsed_roots[:10])}"
    )
    return ok


def test_fork_index_sidecar_builds_index_without_root_or_summary_parse() -> bool:
    _reset_home()
    fork = {
        **_record("child-fork"),
        "parent_session_id": "target-root",
        "fork_point_seq": 0,
    }
    _write(_record("target-root", forks=[fork]))
    _write_summary("target-root", 1, fork_ids=["child-fork"])

    first = session_store._resolve_root_id("child-fork")
    session_store._fork_index.clear()
    session_store._root_forks.clear()
    session_store._root_index_signatures.clear()
    session_store._index_loaded = False
    session_store._index_fingerprint = None

    original_loads = session_store.json.loads
    parsed_session_json: list[str] = []

    def tracking_loads(raw: str, *args, **kwargs):
        data = original_loads(raw, *args, **kwargs)
        if isinstance(data, dict) and data.get("_schema_version") == session_store.SCHEMA_VERSION:
            parsed_session_json.append(data.get("id"))
        return data

    session_store.json.loads = tracking_loads
    try:
        second = session_store._resolve_root_id("child-fork")
    finally:
        session_store.json.loads = original_loads

    ok = first == "target-root" and second == "target-root" and parsed_session_json == []
    print(
        f"{PASS if ok else FAIL} fork-index sidecar avoids root parses"
        f"{'' if ok else ' parsed=' + repr(parsed_session_json[:10])}"
    )
    return ok


def test_index_sidecar_write_happens_outside_index_lock() -> bool:
    _reset_home()
    fork = {
        **_record("child-fork"),
        "parent_session_id": "target-root",
        "fork_point_seq": 0,
    }
    _write(_record("target-root", forks=[fork]))
    _write_summary("target-root", 1, fork_ids=["child-fork"])

    original_write = session_store._write_index_sidecar
    lock_states: list[bool] = []

    def tracking_write(*args, **kwargs):
        acquired = session_store._index_lock.acquire(blocking=False)
        lock_states.append(not acquired)
        if acquired:
            session_store._index_lock.release()
        return original_write(*args, **kwargs)

    session_store._write_index_sidecar = tracking_write
    try:
        resolved = session_store._resolve_root_id("child-fork")
        session_store._index_sidecar_write_queue.join()
    finally:
        session_store._write_index_sidecar = original_write

    ok = resolved == "target-root" and lock_states and not any(lock_states)
    print(
        f"{PASS if ok else FAIL} fork-index sidecar writes outside index lock"
        f"{'' if ok else ' lock_states=' + repr(lock_states)}"
    )
    return ok


def test_fork_index_scan_avoids_path_glob() -> bool:
    _reset_home()
    fork = {
        **_record("child-fork"),
        "parent_session_id": "target-root",
        "fork_point_seq": 0,
    }
    _write(_record("target-root", forks=[fork]))
    _write_summary("target-root", 1, fork_ids=["child-fork"])

    original_glob = Path.glob
    calls = 0

    def fail_glob(self, pattern):
        nonlocal calls
        calls += 1
        raise AssertionError("fork-index scan used Path.glob")

    Path.glob = fail_glob
    try:
        resolved = session_store._resolve_root_id("child-fork")
    finally:
        Path.glob = original_glob

    ok = resolved == "target-root" and calls == 0
    print(
        f"{PASS if ok else FAIL} fork-index scan avoids Path.glob"
        f"{'' if ok else ' calls=' + repr(calls)}"
    )
    return ok


def test_missing_sid_refresh_reuses_fingerprint() -> bool:
    _reset_home()
    _write(_record("target-root"))
    _write_summary("target-root", 0)
    for i in range(20):
        sid = f"other-{i}"
        _write(_record(sid))
        _write_summary(sid, 0)

    original_fingerprint = session_store._dir_fingerprint
    calls = 0

    def tracking_fingerprint():
        nonlocal calls
        calls += 1
        return original_fingerprint()

    session_store._dir_fingerprint = tracking_fingerprint
    try:
        resolved = session_store._resolve_root_id("missing-fork")
    finally:
        session_store._dir_fingerprint = original_fingerprint

    ok = resolved is None and calls <= 3
    print(
        f"{PASS if ok else FAIL} missing sid refresh reuses dir fingerprint"
        f"{'' if ok else ' calls=' + repr(calls)}"
    )
    return ok


def test_concurrent_missing_sid_refresh_attempts_singleflight() -> bool:
    _reset_home()
    _write(_record("target-root"))

    original_build = session_store._build_index_snapshot
    original_fingerprint = session_store._dir_fingerprint
    calls = 0
    calls_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def tracking_build(live_fp=None):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.02)
        return original_build(live_fp)

    def unstable_fingerprint():
        fp = original_fingerprint()
        return (fp[0], fp[1] + time.time_ns(), fp[2])

    def resolve() -> None:
        barrier.wait()
        if session_store._resolve_root_id("missing-fork") is not None:
            raise AssertionError("missing fork resolved")

    session_store._build_index_snapshot = tracking_build
    session_store._dir_fingerprint = unstable_fingerprint
    try:
        threads = [threading.Thread(target=resolve) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        session_store._build_index_snapshot = original_build
        session_store._dir_fingerprint = original_fingerprint

    ok = calls == 1
    print(
        f"{PASS if ok else FAIL} unstable index refresh performs one full build"
        f"{'' if ok else ' calls=' + repr(calls)}"
    )
    return ok


def test_concurrent_dir_fingerprint_cache_singleflights() -> bool:
    _reset_home()
    _write(_record("target-root"))

    original_fingerprint = session_store._dir_fingerprint
    session_store._dir_fingerprint_cache = None
    calls = 0
    calls_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def slow_fingerprint():
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.02)
        return original_fingerprint()

    def fingerprint() -> None:
        barrier.wait()
        session_store._dir_fingerprint_cached()

    session_store._dir_fingerprint = slow_fingerprint
    try:
        threads = [threading.Thread(target=fingerprint) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        session_store._dir_fingerprint = original_fingerprint
        session_store._dir_fingerprint_cache = None

    ok = calls == 1
    print(
        f"{PASS if ok else FAIL} concurrent dir fingerprint cache singleflights"
        f"{'' if ok else ' calls=' + repr(calls)}"
    )
    return ok


def test_write_session_full_updates_loaded_fork_index_sidecar() -> bool:
    _reset_home()
    root = _record("target-root")
    _write(root)
    _write_summary("target-root", 0)

    session_store._ensure_index()
    fork = {
        **_record("child-fork"),
        "parent_session_id": "target-root",
        "fork_point_seq": 0,
    }
    root["forks"] = [fork]
    session_store.write_session_full(root, bump_updated_at=False)

    session_store._fork_index.clear()
    session_store._root_forks.clear()
    session_store._root_index_signatures.clear()
    session_store._index_loaded = False
    session_store._index_fingerprint = None

    original_loads = session_store.json.loads
    parsed_session_json: list[str] = []

    def tracking_loads(raw: str, *args, **kwargs):
        data = original_loads(raw, *args, **kwargs)
        if isinstance(data, dict) and data.get("_schema_version") == session_store.SCHEMA_VERSION:
            parsed_session_json.append(data.get("id"))
        return data

    session_store.json.loads = tracking_loads
    try:
        resolved = session_store._resolve_root_id("child-fork")
    finally:
        session_store.json.loads = original_loads

    ok = resolved == "target-root" and parsed_session_json == []
    print(
        f"{PASS if ok else FAIL} write_session_full refreshes fork-index sidecar"
        f"{'' if ok else ' parsed=' + repr(parsed_session_json[:10])}"
    )
    return ok


def test_loaded_fork_sidecar_update_skips_dir_fingerprint_scan() -> bool:
    _reset_home()
    root = _record("target-root")
    _write(root)
    _write_summary("target-root", 0)
    session_store._ensure_index()
    session_store._index_sidecar_write_queue.join()

    fork = {
        **_record("child-fork"),
        "parent_session_id": "target-root",
        "fork_point_seq": 0,
    }
    root["forks"] = [fork]

    original_fingerprint = session_store._dir_fingerprint
    calls = 0

    def tracking_fingerprint():
        nonlocal calls
        calls += 1
        return original_fingerprint()

    session_store._dir_fingerprint = tracking_fingerprint
    try:
        session_store.write_session_full(root, bump_updated_at=False)
        session_store._index_sidecar_write_queue.join()
    finally:
        session_store._dir_fingerprint = original_fingerprint

    ok = calls == 0 and session_store._resolve_root_id("child-fork") == "target-root"
    print(
        f"{PASS if ok else FAIL} loaded fork sidecar update skips dir fingerprint scan"
        f"{'' if ok else ' calls=' + repr(calls)}"
    )
    return ok


def test_loaded_fork_metadata_write_skips_fork_index_sidecar() -> bool:
    _reset_home()
    fork = {
        **_record("child-fork"),
        "parent_session_id": "target-root",
        "fork_point_seq": 0,
    }
    root = _record("target-root", forks=[fork])
    _write(root)
    _write_summary("target-root", 1, fork_ids=["child-fork"])
    session_store._ensure_index()
    session_store._index_sidecar_write_queue.join()
    sidecar = Path(_TMP_HOME) / "sessions" / ".fork-index.json"
    before = sidecar.stat().st_mtime_ns

    root["messages"] = [{"role": "user", "content": "hello"}]
    session_store.write_session_full(root, bump_updated_at=False)
    session_store._index_sidecar_write_queue.join()
    after = sidecar.stat().st_mtime_ns

    ok = before == after and session_store._resolve_root_id("child-fork") == "target-root"
    print(
        f"{PASS if ok else FAIL} loaded fork metadata write skips fork-index sidecar"
        f"{'' if ok else f' before={before} after={after}'}"
    )
    return ok


def test_write_session_full_skips_fork_index_sidecar_for_metadata_only_write() -> bool:
    _reset_home()
    root = _record("target-root")
    _write(root)
    _write_summary("target-root", 0)
    session_store._ensure_index()
    session_store._index_sidecar_write_queue.join()
    sidecar = Path(_TMP_HOME) / "sessions" / ".fork-index.json"
    before = sidecar.stat().st_mtime_ns

    root["name"] = "metadata-only"
    session_store.write_session_full(root, bump_updated_at=False)
    after = sidecar.stat().st_mtime_ns

    ok = before == after
    print(
        f"{PASS if ok else FAIL} metadata-only write skips fork-index sidecar"
        f"{'' if ok else f' before={before} after={after}'}"
    )
    return ok


def test_write_session_full_updates_unloaded_fork_index_sidecar() -> bool:
    _reset_home()
    fork = {
        **_record("child-fork"),
        "parent_session_id": "target-root",
        "fork_point_seq": 0,
    }
    root = _record("target-root", forks=[fork])
    _write(root)
    _write_summary("target-root", 1, fork_ids=["child-fork"])

    first = session_store._resolve_root_id("child-fork")
    session_store._fork_index.clear()
    session_store._root_forks.clear()
    session_store._root_index_signatures.clear()
    session_store._index_loaded = False
    session_store._index_fingerprint = None

    root["name"] = "changed-before-index-load"
    session_store.write_session_full(root, bump_updated_at=False)

    session_store._fork_index.clear()
    session_store._root_forks.clear()
    session_store._root_index_signatures.clear()
    session_store._index_loaded = False
    session_store._index_fingerprint = None

    original_loads = session_store.json.loads
    parsed_session_json: list[str] = []

    def tracking_loads(raw: str, *args, **kwargs):
        data = original_loads(raw, *args, **kwargs)
        if isinstance(data, dict) and data.get("_schema_version") == session_store.SCHEMA_VERSION:
            parsed_session_json.append(data.get("id"))
        return data

    session_store.json.loads = tracking_loads
    try:
        resolved = session_store._resolve_root_id("child-fork")
    finally:
        session_store.json.loads = original_loads

    ok = first == "target-root" and resolved == "target-root" and parsed_session_json == []
    print(
        f"{PASS if ok else FAIL} unloaded write refreshes fork-index sidecar"
        f"{'' if ok else ' parsed=' + repr(parsed_session_json[:10])}"
    )
    return ok


def test_legacy_fork_summary_backfills_fork_ids() -> bool:
    _reset_home()
    fork = {
        **_record("child-fork"),
        "parent_session_id": "target-root",
        "fork_point_seq": 0,
    }
    _write(_record("target-root", forks=[fork]))
    _write_summary("target-root", 1, fork_ids=None)
    summary_path = Path(_TMP_HOME) / "sessions" / "target-root.summary.json"
    legacy = json.loads(summary_path.read_text(encoding="utf-8"))
    legacy.pop("fork_ids", None)
    summary_path.write_text(json.dumps(legacy), encoding="utf-8")

    session_store._ensure_summary_index(blocking=True)

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    ok = summary.get("fork_ids") == ["child-fork"]
    print(f"{PASS if ok else FAIL} legacy fork summary backfills fork_ids")
    return ok


def test_stale_zero_fork_summary_still_scans_root() -> bool:
    _reset_home()
    fork = {
        **_record("child-fork"),
        "parent_session_id": "target-root",
        "fork_point_seq": 0,
    }
    _write(_record("target-root", forks=[fork]))
    _write_summary("target-root", 0, stale=True)

    resolved = session_store._resolve_root_id("child-fork")
    ok = resolved == "target-root"
    print(f"{PASS if ok else FAIL} stale zero-fork summary still scans root")
    return ok


def test_summary_build_refreshes_stale_summary_file() -> bool:
    _reset_home()
    _write(_record("target-root"))
    _write_summary("target-root", 0, stale=True)
    path = Path(_TMP_HOME) / "sessions" / "target-root.json"
    summary_path = Path(_TMP_HOME) / "sessions" / "target-root.summary.json"

    session_store._ensure_summary_index(blocking=True)

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    ok = (
        summary.get("id") == "target-root"
        and summary_path.stat().st_mtime_ns >= path.stat().st_mtime_ns
    )
    print(f"{PASS if ok else FAIL} summary build refreshes stale summary file")
    return ok


def main() -> int:
    try:
        checks = [
            test_root_id_resolves_without_cold_scan(),
            test_fork_id_still_uses_index(),
            test_concurrent_fork_misses_singleflight_cold_build(),
            test_fresh_zero_fork_summaries_skip_full_parse(),
            test_fresh_fork_summary_builds_index_without_root_parse(),
            test_fork_index_sidecar_builds_index_without_root_or_summary_parse(),
            test_index_sidecar_write_happens_outside_index_lock(),
            test_fork_index_scan_avoids_path_glob(),
            test_missing_sid_refresh_reuses_fingerprint(),
            test_concurrent_missing_sid_refresh_attempts_singleflight(),
            test_concurrent_dir_fingerprint_cache_singleflights(),
            test_write_session_full_updates_loaded_fork_index_sidecar(),
            test_loaded_fork_sidecar_update_skips_dir_fingerprint_scan(),
            test_loaded_fork_metadata_write_skips_fork_index_sidecar(),
            test_write_session_full_skips_fork_index_sidecar_for_metadata_only_write(),
            test_write_session_full_updates_unloaded_fork_index_sidecar(),
            test_legacy_fork_summary_backfills_fork_ids(),
            test_stale_zero_fork_summary_still_scans_root(),
            test_summary_build_refreshes_stale_summary_file(),
        ]
        passed = sum(1 for ok in checks if ok)
        print(f"\n{passed}/{len(checks)} checks passed")
        return 0 if passed == len(checks) else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
