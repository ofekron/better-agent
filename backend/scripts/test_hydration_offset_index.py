from __future__ import annotations

import gc
import json
import os
import shutil
import threading
import time
import tracemalloc
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import _test_home

_HOME = _test_home.isolate("bc-test-hydration-index-")

import render_tree_hydrate as hydrate  # noqa: E402
from event_ingester import event_ingester  # noqa: E402


def _row(seq: int, sid: str, msg_id: str | None = None) -> dict:
    return {
        "seq": seq,
        "sid": sid,
        "msg_id": msg_id,
        "type": "agent_message",
        "source": "test",
        "data": {"uuid": f"u-{seq}", "text": "x" * 32},
    }


def _write(path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(
        json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in rows
    ))


def main() -> int:
    manager_source = (Path(__file__).resolve().parent.parent / "session_manager.py").read_text()
    lease_start = manager_source.index("    def hydrate_root_prepared(")
    lease_end = manager_source.index("    def _derive_current_todos_from_events_jsonl(", lease_start)
    lease_source = manager_source[lease_start:lease_end]
    ownership_check = lease_source.index("ownership_validated = validate_prepared_ownership")
    apply_lock = lease_source.index("with self._lock_for_root(rid):", ownership_check)
    assert lease_source.index("slot = hydration_decode_apply_slot()") < ownership_check < apply_lock
    assert "_ownership_snapshot" not in lease_source[apply_lock:]

    root = "root"
    path = event_ingester._events_path(root)
    rows = [_row(i, "root" if i % 2 else "fork", "m") for i in range(1, 1001)]
    _write(path, rows)
    original_resolutions = event_ingester.ownership_resolutions
    event_ingester.ownership_resolutions = lambda _root: {}
    hydrate._hydration_indexes.clear()
    builds = 0
    build_lock = threading.Lock()
    original_build = hydrate._build_hydration_index

    canonical = json.dumps({
        "seq": 1, "ts": "2026-07-17T00:00:00+00:00",
        "sid": 'sid-with-"quote', "msg_id": "m", "type": "agent_message",
        "source": "test", "data": {"uuid": "u-1"},
    }, separators=(",", ":")).encode() + b"\n"
    fast_sid, used_fast_path = hydrate.hydration_index_store._sid_from_line(canonical)
    assert (fast_sid, used_fast_path) == ('sid-with-"quote', True)
    reordered = json.dumps(
        {"data": {"payload": "x" * 8192}, **_row(2, "legacy", "m")},
        separators=(",", ":"),
    ).encode() + b"\n"
    fallback_sid, used_fast_path = hydrate.hydration_index_store._sid_from_line(reordered)
    assert (fallback_sid, used_fast_path) == ("legacy", False)

    perf_journal = path.with_name("large-payload-events.jsonl")
    payload = "x" * (256 * 1024)
    _write(perf_journal, [{
        "seq": seq, "ts": "2026-07-17T00:00:00+00:00",
        "sid": "large-payload", "msg_id": "m", "type": "agent_message",
        "source": "test", "data": {"uuid": f"u-{seq}", "payload": payload},
    } for seq in range(1, 257)])
    perf_db = path.with_name("large-payload-offsets.sqlite3")
    perf_conn = hydrate.hydration_index_store._create(perf_db)
    original_json_loads = hydrate.hydration_index_store.json.loads
    largest_decoded_input = 0

    def measured_json_loads(value, *args, **kwargs):
        nonlocal largest_decoded_input
        if isinstance(value, (bytes, bytearray, str)):
            largest_decoded_input = max(largest_decoded_input, len(value))
        return original_json_loads(value, *args, **kwargs)

    hydrate.hydration_index_store.json.loads = measured_json_loads
    started = time.perf_counter()
    try:
        committed, scanned, inserted, _digest = hydrate.hydration_index_store._scan(
            perf_conn, perf_journal, 0, bytes(32).hex(),
        )
    finally:
        hydrate.hydration_index_store.json.loads = original_json_loads
        perf_conn.close()
    elapsed = time.perf_counter() - started
    assert committed == scanned == perf_journal.stat().st_size
    assert inserted == 256
    assert largest_decoded_input < 256, largest_decoded_input
    assert elapsed < 2.0, elapsed
    perf_journal.unlink()
    perf_db.unlink()

    def counted_build(*args, **kwargs):
        nonlocal builds
        with build_lock:
            builds += 1
        return original_build(*args, **kwargs)

    hydrate._build_hydration_index = counted_build
    tracemalloc.start()
    try:
        with ThreadPoolExecutor(max_workers=50) as pool:
            results = list(pool.map(
                lambda _i: hydrate._indexed_rows_for_sid(root, "root"), range(50),
            ))
        _current, peak = tracemalloc.get_traced_memory()
        assert builds == 1, builds
        assert all(len(result) == 500 for result in results)
        results[0][0]["data"]["text"] = "mutated"
        assert results[1][0]["data"]["text"] != "mutated"
        index = hydrate._hydration_indexes[root]
        assert all(isinstance(offset, int) for offsets in index.offsets_by_sid.values() for offset in offsets)
        assert peak < 80 * 1024 * 1024, peak

        fork_rows = hydrate._indexed_rows_for_sid(root, "fork")
        assert len(fork_rows) == 500 and all(row["sid"] == "fork" for row in fork_rows)

        event_ingester._root_events_version[root] = 1
        event_ingester.ownership_resolutions = lambda _root: {1: "owned"}
        owned = hydrate._indexed_rows_for_sid(root, "root")
        assert owned[0]["msg_id"] == "owned"
        assert builds == 2

        old_stat = path.stat()
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(path.read_bytes().replace(b'"text":"', b'"text":"', 1))
        assert replacement.stat().st_size == old_stat.st_size
        os.replace(replacement, path)
        os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        hydrate._indexed_rows_for_sid(root, "root")
        assert builds == 3

        _write(path, [_row(1, "root", "m")])
        truncated = hydrate._indexed_rows_for_sid(root, "root")
        assert len(truncated) == 1 and builds == 4

        original_index = hydrate._hydration_index
        injected = False

        def append_after_index_return(root_id):
            nonlocal injected
            result = original_index(root_id)
            if not injected:
                injected = True
                with path.open("ab") as file:
                    file.write(json.dumps(_row(2, "root", "m"), separators=(",", ":")).encode() + b"\n")
            return result

        hydrate._hydration_index = append_after_index_return
        try:
            changed_during_read = hydrate._indexed_rows_for_sid(root, "root")
        finally:
            hydrate._hydration_index = original_index
        assert [row["seq"] for row in changed_during_read] == [1, 2]

        with path.open("wb") as file:
            for seq in range(1, 165_001):
                file.write(json.dumps(
                    _row(seq, "root" if seq % 2 else "fork", "m"),
                    separators=(",", ":"),
                ).encode() + b"\n")
        hydrate._indexed_rows_for_sid(root, "missing")
        large_index = hydrate._hydration_indexes[root]
        assert sum(map(len, large_index.offsets_by_sid.values())) == 165_000
        assert all(isinstance(offset, int) for offsets in large_index.offsets_by_sid.values() for offset in offsets)
        _current, large_peak = tracemalloc.get_traced_memory()
        assert large_peak < 180 * 1024 * 1024, large_peak

        active = 0
        max_active = 0
        concurrency_lock = threading.Lock()
        original_hydrate = hydrate._hydrate_msg_events_from_jsonl

        def bounded_probe(*args, **kwargs):
            nonlocal active, max_active
            with concurrency_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.01)
            with concurrency_lock:
                active -= 1

        hydrate._hydrate_msg_events_from_jsonl = bounded_probe
        try:
            with ThreadPoolExecutor(max_workers=50) as pool:
                list(pool.map(lambda _i: hydrate.hydrate_msg_events_from_jsonl({}), range(50)))
        finally:
            hydrate._hydrate_msg_events_from_jsonl = original_hydrate
        assert max_active <= 2, max_active

        from session_manager import manager as session_manager
        original_prepare = hydrate.prepare_hydration
        original_decode = hydrate.decode_prepared_hydration
        original_validate = hydrate.validate_prepared_ownership
        original_apply = hydrate.apply_prepared_hydration
        manager_roots = [f"manager-root-{index}" for index in range(50)]
        manager_active = 0
        manager_max_active = 0

        def fake_prepare(root_id, tree_sids, *, after_seq=0):
            return types.SimpleNamespace(root_id=root_id)

        def fake_decode(_prepared):
            nonlocal manager_active, manager_max_active
            with concurrency_lock:
                manager_active += 1
                manager_max_active = max(manager_max_active, manager_active)
            time.sleep(0.01)
            with concurrency_lock:
                manager_active -= 1
            return {}

        hydrate.prepare_hydration = fake_prepare
        hydrate.decode_prepared_hydration = fake_decode
        hydrate.validate_prepared_ownership = lambda _prepared: True
        hydrate.apply_prepared_hydration = lambda *_args, **_kwargs: True
        try:
            session_manager._ensure_home_current()
            for manager_root in manager_roots:
                manager_path = event_ingester._events_path(manager_root)
                manager_path.parent.mkdir(parents=True, exist_ok=True)
                manager_path.touch()
                session_manager._roots[manager_root] = {
                    "id": manager_root, "messages": [], "forks": [],
                }
                session_manager._node_root_id[manager_root] = manager_root
            with ThreadPoolExecutor(max_workers=50) as pool:
                outcomes = list(pool.map(
                    lambda manager_root: session_manager.hydrate_root_prepared(
                        manager_root, after_seq=1,
                    ),
                    manager_roots,
                ))
            assert all(outcomes), outcomes
            assert manager_max_active <= 2, manager_max_active
        finally:
            hydrate.prepare_hydration = original_prepare
            hydrate.decode_prepared_hydration = original_decode
            hydrate.validate_prepared_ownership = original_validate
            hydrate.apply_prepared_hydration = original_apply
            for manager_root in manager_roots:
                session_manager._roots.pop(manager_root, None)
                session_manager._node_root_id.pop(manager_root, None)
        del results, fork_rows, owned, truncated
        gc.collect()
        assert len(hydrate._hydration_indexes) <= hydrate._HYDRATION_INDEX_LIMIT
    finally:
        tracemalloc.stop()
        hydrate._build_hydration_index = original_build
        event_ingester.ownership_resolutions = original_resolutions
        event_ingester._root_events_version.pop(root, None)
        shutil.rmtree(_HOME, ignore_errors=True)
    print("PASS: bounded hydration offset index")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
