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
from root_change_wal import RootChange, RootChangeOwner, RootChangeWal  # noqa: E402

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
    session_store._index_generation = 0
    session_store._dir_fingerprint_cache = None
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
    root_signature = session_store._session_file_signature(
        sessions_dir / f"{sid}.json",
    )
    path.write_text(
        json.dumps({
            "id": sid,
            "updated_at": "2026-06-22T00:00:00+00:00",
            "fork_count": fork_count,
            "fork_ids": fork_ids if fork_ids is not None else [],
            "last_seen_event_uid": None,
            "current_todos": [],
            "current_tasks": [],
            "_root_file_signature": list(root_signature) if root_signature else None,
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


def test_missing_sid_never_scans_directory_fingerprint() -> bool:
    _reset_home()
    _write(_record("target-root"))
    _write_summary("target-root", 0)
    for i in range(20):
        sid = f"other-{i}"
        _write(_record(sid))
        _write_summary(sid, 0)
    session_store._ensure_index()

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

    ok = resolved is None and calls == 0
    print(
        f"{PASS if ok else FAIL} missing sid never scans dir fingerprint"
        f"{'' if ok else ' calls=' + repr(calls)}"
    )
    return ok


def test_projection_between_miss_and_generation_capture_resolves() -> bool:
    _reset_home()
    _write(_record("target-root"))
    _write_summary("target-root", 0)
    session_store._ensure_index()
    original_owner = session_store._root_change_owner

    class RacingOwner:
        def wait_ready(self):
            return None

        @property
        def observation_generation(self):
            with session_store._index_lock:
                session_store._fork_index["racing-fork"] = "target-root"
            return 7

        def wait_for_observation(self, generation, timeout):
            assert generation == 7
            return False

    session_store._root_change_owner = RacingOwner()
    try:
        resolved = session_store._resolve_root_id("racing-fork")
    finally:
        session_store._root_change_owner = original_owner
    ok = resolved == "target-root"
    print(
        f"{PASS if ok else FAIL} projection before generation capture resolves"
        f"{'' if ok else ' resolved=' + repr(resolved)}"
    )
    return ok


def test_projection_during_timed_out_observation_wait_resolves() -> bool:
    _reset_home()
    _write(_record("target-root"))
    _write_summary("target-root", 0)
    session_store._ensure_index()
    original_owner = session_store._root_change_owner

    class TimingOutOwner:
        def wait_ready(self):
            return None

        observation_generation = 9

        def wait_for_observation(self, generation, timeout):
            with session_store._index_lock:
                session_store._fork_index["timeout-fork"] = "target-root"
            return False

    session_store._root_change_owner = TimingOutOwner()
    try:
        resolved = session_store._resolve_root_id("timeout-fork")
    finally:
        session_store._root_change_owner = original_owner
    ok = resolved == "target-root"
    print(f"{PASS if ok else FAIL} projection during timed-out wait resolves")
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
        return (fp[0], fp[1] + time.time_ns(), fp[2], fp[3], fp[4])

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


def test_blocked_fingerprint_publication_keeps_loaded_root_lookup_responsive() -> bool:
    _reset_home()
    root = _record("target-root")
    _write(root)
    _write_summary("target-root", 0)
    session_store._ensure_index()

    writer_started = threading.Event()
    writer_index_phase = threading.Event()
    lookup_done = threading.Event()
    result: dict[str, object] = {}
    original_after_write = session_store._fingerprint_after_root_write_locked

    def observe_index_phase(previous_signature, file_signature, root_id):
        writer_index_phase.set()
        return original_after_write(previous_signature, file_signature, root_id)

    def write_root() -> None:
        writer_started.set()
        session_store.write_session_full(root, bump_updated_at=False)

    def lookup_root() -> None:
        result["resolved"] = session_store._loaded_root_id_for("target-root")
        lookup_done.set()

    session_store._dir_fingerprint_cache_lock.acquire()
    session_store._fingerprint_after_root_write_locked = observe_index_phase
    writer = threading.Thread(target=write_root)
    lookup = threading.Thread(target=lookup_root)
    try:
        writer.start()
        if not writer_started.wait(timeout=1):
            raise AssertionError("writer did not start")
        if not writer_index_phase.wait(timeout=1):
            raise AssertionError("writer did not reach index publication")
        lookup.start()
        responsive = lookup_done.wait(timeout=0.25)
    finally:
        session_store._dir_fingerprint_cache_lock.release()
        writer.join(timeout=2)
        lookup.join(timeout=2)
        session_store._fingerprint_after_root_write_locked = original_after_write

    ok = (
        responsive
        and result.get("resolved") == "target-root"
        and not writer.is_alive()
        and not lookup.is_alive()
    )
    print(
        f"{PASS if ok else FAIL} blocked fingerprint publish keeps index lookup responsive"
        f"{'' if ok else f' responsive={responsive} result={result!r}'}"
    )
    return ok


def test_mutation_between_scan_and_publish_rejects_stale_fingerprint() -> bool:
    _reset_home()
    root = _record("target-root")
    _write(root)
    _write_summary("target-root", 0)
    session_store._ensure_index()
    with session_store._index_lock:
        stale_fingerprint = session_store._index_fingerprint
        stale_generation = session_store._index_generation
        session_store._root_index_signatures["new-root"] = (1, 1, 1, 1, 1)
        session_store._index_fingerprint = (
            stale_fingerprint[0] + 1,
            max(stale_fingerprint[1], 1),
            stale_fingerprint[2],
            stale_fingerprint[3] + 1,
            stale_fingerprint[4] ^ 1,
        )
        current_fingerprint = session_store._index_fingerprint
        session_store._bump_index_generation_locked()
    session_store._dir_fingerprint_cache = None

    published = session_store._publish_dir_fingerprint_cache(
        stale_fingerprint,
        stale_generation,
    )
    ok = (
        not published
        and session_store._index_fingerprint == current_fingerprint
        and session_store._dir_fingerprint_cache is None
    )
    print(
        f"{PASS if ok else FAIL} stale fingerprint publication is rejected"
    )
    return ok


def test_sidecar_scan_retries_after_concurrent_index_mutation() -> bool:
    _reset_home()
    _write(_record("target-root"))
    _write_summary("target-root", 0)
    session_store._ensure_index()

    original_fingerprint = session_store._dir_fingerprint
    calls = 0

    def tracked_fingerprint():
        nonlocal calls
        calls += 1
        return original_fingerprint()

    session_store._dir_fingerprint = tracked_fingerprint
    try:
        session_store._persist_index_sidecar_if_loaded()
    finally:
        session_store._dir_fingerprint = original_fingerprint

    session_store._index_sidecar_write_queue.join()
    payload = session_store._read_index_sidecar_payload()
    parsed = session_store._parse_index_sidecar(payload) if payload else None
    sidecar_signatures = parsed[2] if parsed is not None else {}
    ok = calls == 0 and "target-root" in sidecar_signatures
    print(
        f"{PASS if ok else FAIL} sidecar snapshot performs no fingerprint scan"
        f"{'' if ok else f' calls={calls} signatures={sidecar_signatures!r}'}"
    )
    return ok


def test_external_fork_write_reconciles_before_sidecar_publish() -> bool:
    _reset_home()
    _write(_record("target-root"))
    _write_summary("target-root", 0)
    session_store._ensure_index()

    fork = {
        **_record("external-fork"),
        "parent_session_id": "target-root",
        "fork_point_seq": 0,
    }
    _write(_record("target-root", forks=[fork]))
    _write_summary("target-root", 1, fork_ids=["external-fork"])
    session_store.project_external_root_change("target-root")
    session_store._persist_index_sidecar_if_loaded()
    session_store._index_sidecar_write_queue.join()

    with session_store._index_lock:
        session_store._fork_index.clear()
        session_store._root_forks.clear()
        session_store._root_index_signatures.clear()
        session_store._index_loaded = False
        session_store._index_fingerprint = None
        session_store._bump_index_generation_locked()
    session_store._dir_fingerprint_cache = None

    resolved = session_store._resolve_root_id("external-fork")
    ok = resolved == "target-root"
    print(
        f"{PASS if ok else FAIL} external fork is reconciled before sidecar publish"
        f"{'' if ok else f' resolved={resolved!r}'}"
    )
    return ok


def test_preserved_mtime_size_topology_rewrite_invalidates_sidecar() -> bool:
    _reset_home()
    original_change_identity = session_store._file_change_identity
    change_identity = {"value": 101}

    def platform_change_identity(_path, _stat_result):
        return change_identity["value"]

    session_store._file_change_identity = platform_change_identity
    old_fork = {
        **_record("old-fork-id"),
        "parent_session_id": "target-root",
        "fork_point_seq": 0,
    }
    old_root = _record("target-root", forks=[old_fork])
    try:
        _write(old_root)
        _write_summary("target-root", 1, fork_ids=["old-fork-id"])
        session_store._ensure_index()
        session_store._index_sidecar_write_queue.join()

        path = Path(_TMP_HOME) / "sessions" / "target-root.json"
        before = path.stat()
        old_signature = session_store._session_file_signature(path)
        new_fork = {
            **_record("new-fork-id"),
            "parent_session_id": "target-root",
            "fork_point_seq": 0,
        }
        old_bytes = json.dumps(old_root).encode("utf-8")
        new_bytes = json.dumps(_record("target-root", forks=[new_fork])).encode("utf-8")
        assert len(new_bytes) == len(old_bytes)
        path.write_bytes(new_bytes)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        change_identity["value"] = 202
        after = path.stat()
        new_signature = session_store._session_file_signature(path)
        assert after.st_dev == before.st_dev
        assert after.st_ino == before.st_ino
        assert after.st_size == before.st_size
        assert after.st_mtime_ns == before.st_mtime_ns
        assert old_signature[:2] + old_signature[3:] == new_signature[:2] + new_signature[3:]
        assert old_signature[2] != new_signature[2]

        with session_store._index_lock:
            session_store._fork_index.clear()
            session_store._root_forks.clear()
            session_store._root_index_signatures.clear()
            session_store._index_loaded = False
            session_store._index_fingerprint = None
            session_store._bump_index_generation_locked()
        session_store._dir_fingerprint_cache = None

        new_owner = session_store._resolve_root_id("new-fork-id")
        old_owner = session_store._resolve_root_id("old-fork-id")
    finally:
        session_store._file_change_identity = original_change_identity
    ok = new_owner == "target-root" and old_owner is None
    print(
        f"{PASS if ok else FAIL} preserved-metadata topology rewrite invalidates sidecar"
        f"{'' if ok else f' new={new_owner!r} old={old_owner!r}'}"
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


def test_root_change_projection_accepts_only_vanished_upsert() -> bool:
    _reset_home()
    sid = "vanished-root"
    _write(_record(sid))
    path = Path(_TMP_HOME) / "sessions" / f"{sid}.json"
    signature = session_store._session_file_signature(path)
    wal_path = Path(_TMP_HOME) / "indexes" / "vanished-upsert.sqlite3"
    wal = RootChangeWal(wal_path)
    wal.open()
    wal.append_many((
        ("upsert", sid, path, signature),
        ("delete", sid, path, None),
    ))
    wal.close()
    path.unlink()

    owner = RootChangeOwner(
        wal=RootChangeWal(wal_path),
        roots=lambda: (),
        apply=session_store._apply_root_change,
        poll_interval_s=60,
    )
    owner.start()
    owner.wait_ready(3)
    owner.stop()
    inspection = RootChangeWal(wal_path)
    inspection.open()
    checkpoint = inspection.checkpoint("session-root-projection")
    inspection.close()

    path.write_text("{", encoding="utf-8")
    malformed = session_store._apply_root_change(
        RootChange(
            3,
            "upsert",
            sid,
            path,
            session_store._session_file_signature(path),
        ),
    )

    ok = checkpoint == 2 and malformed is False
    print(f"{PASS if ok else FAIL} projection accepts only vanished WAL upsert")
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
            test_missing_sid_never_scans_directory_fingerprint(),
            test_projection_between_miss_and_generation_capture_resolves(),
            test_projection_during_timed_out_observation_wait_resolves(),
            test_concurrent_missing_sid_refresh_attempts_singleflight(),
            test_concurrent_dir_fingerprint_cache_singleflights(),
            test_blocked_fingerprint_publication_keeps_loaded_root_lookup_responsive(),
            test_mutation_between_scan_and_publish_rejects_stale_fingerprint(),
            test_sidecar_scan_retries_after_concurrent_index_mutation(),
            test_external_fork_write_reconciles_before_sidecar_publish(),
            test_preserved_mtime_size_topology_rewrite_invalidates_sidecar(),
            test_write_session_full_updates_loaded_fork_index_sidecar(),
            test_loaded_fork_sidecar_update_skips_dir_fingerprint_scan(),
            test_loaded_fork_metadata_write_skips_fork_index_sidecar(),
            test_write_session_full_skips_fork_index_sidecar_for_metadata_only_write(),
            test_write_session_full_updates_unloaded_fork_index_sidecar(),
            test_legacy_fork_summary_backfills_fork_ids(),
            test_stale_zero_fork_summary_still_scans_root(),
            test_summary_build_refreshes_stale_summary_file(),
            test_root_change_projection_accepts_only_vanished_upsert(),
        ]
        passed = sum(1 for ok in checks if ok)
        print(f"\n{passed}/{len(checks)} checks passed")
        return 0 if passed == len(checks) else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
