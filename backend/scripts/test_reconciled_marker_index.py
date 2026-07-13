from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-reconciled-index-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import provider  # noqa: E402
import runs_dir  # noqa: E402
from ingestion_versions import (  # noqa: E402
    CLAUDE_INGESTION_VERSION,
    current_ingestion_version,
    write_marker,
)
from runs_dir import runs_root  # noqa: E402


def _reset_runs() -> Path:
    root = runs_root()
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _run_dir(root: Path, run_id: str, *, provider_id: str = "claude-main") -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "backend_state.json").write_text(
        json.dumps({"provider_id": provider_id}),
        encoding="utf-8",
    )
    return run_dir


def _recover_with_no_fallback_provider() -> list[dict]:
    with mock.patch.object(provider, "default_provider", side_effect=AssertionError):
        return provider.recover_all_in_flight()


def test_indexed_current_run_skips_marker_json_and_backend_state() -> None:
    root = _reset_runs()
    indexed = _run_dir(root, "run-indexed")
    write_marker(indexed / "reconciled.marker", "claude")
    pending = _run_dir(root, "run-pending")

    original_read_text = Path.read_text

    def guarded_read_text(path: Path, *args, **kwargs):
        if path == indexed / "reconciled.marker":
            raise AssertionError("indexed marker JSON should not be read")
        if path == indexed / "backend_state.json":
            raise AssertionError("indexed backend_state should not be read")
        return original_read_text(path, *args, **kwargs)

    with mock.patch.object(Path, "read_text", guarded_read_text):
        found = _recover_with_no_fallback_provider()

    assert found == []
    print("PASS indexed current run skips marker JSON and backend_state")


def test_index_missing_marker_does_not_skip() -> None:
    root = _reset_runs()
    run_dir = _run_dir(root, "run-missing-marker")
    write_marker(run_dir / "reconciled.marker", "claude")
    (run_dir / "reconciled.marker").unlink()

    original_read_text = Path.read_text
    reads: list[Path] = []

    def tracking_read_text(path: Path, *args, **kwargs):
        reads.append(path)
        return original_read_text(path, *args, **kwargs)

    with mock.patch.object(Path, "read_text", tracking_read_text):
        found = _recover_with_no_fallback_provider()

    assert found == []
    assert run_dir / "reconciled.marker" in reads
    assert run_dir / "backend_state.json" not in reads
    print("PASS indexed row with missing marker does not skip")


def test_index_stale_signature_does_not_skip() -> None:
    root = _reset_runs()
    run_dir = _run_dir(root, "run-rewritten-marker")
    write_marker(run_dir / "reconciled.marker", "claude")
    time.sleep(0.001)
    (run_dir / "reconciled.marker").write_text(
        json.dumps({
            "provider_kind": "claude",
            "ingestion_version": CLAUDE_INGESTION_VERSION - 1,
            "changed": "signature",
        }),
        encoding="utf-8",
    )
    future = time.time() + 1
    os.utime(run_dir / "reconciled.marker", (future, future))

    original_read_text = Path.read_text
    reads: list[Path] = []

    def tracking_read_text(path: Path, *args, **kwargs):
        reads.append(path)
        return original_read_text(path, *args, **kwargs)

    with mock.patch.object(Path, "read_text", tracking_read_text):
        found = _recover_with_no_fallback_provider()

    assert found == []
    assert run_dir / "reconciled.marker" in reads
    assert run_dir / "backend_state.json" not in reads
    indexed = runs_dir.load_reconciled_marker_index(root)
    assert indexed["run-rewritten-marker"]["ingestion_version"] == CLAUDE_INGESTION_VERSION - 1
    print("PASS indexed row with stale marker signature does not skip")


def test_stale_ingestion_version_row_does_not_skip() -> None:
    root = _reset_runs()
    run_dir = _run_dir(root, "run-stale-version")
    marker = run_dir / "reconciled.marker"
    marker.write_text(
        json.dumps({
            "provider_kind": "claude",
            "ingestion_version": CLAUDE_INGESTION_VERSION - 1,
        }),
        encoding="utf-8",
    )
    runs_dir.append_reconciled_marker_index(
        marker,
        "claude",
        CLAUDE_INGESTION_VERSION - 1,
        root=root,
    )

    original_read_text = Path.read_text
    reads: list[Path] = []

    def tracking_read_text(path: Path, *args, **kwargs):
        reads.append(path)
        return original_read_text(path, *args, **kwargs)

    with mock.patch.object(Path, "read_text", tracking_read_text):
        found = _recover_with_no_fallback_provider()

    assert found == []
    assert run_dir / "reconciled.marker" in reads
    print("PASS stale ingestion version row does not skip")


def test_backfill_skips_symlink_run_dir() -> None:
    root = _reset_runs()
    outside = Path(tempfile.mkdtemp(prefix="bc-reconciled-outside-"))
    try:
        _run_dir(outside, "outside-run")
        write_marker(outside / "outside-run" / "reconciled.marker", "claude")
        (root / "run-symlink").symlink_to(outside / "outside-run", target_is_directory=True)
        assert runs_dir.ensure_reconciled_marker_index_backfilled(root) is True
        assert runs_dir.load_reconciled_marker_index(root) == {}
    finally:
        shutil.rmtree(outside, ignore_errors=True)
    print("PASS backfill skips symlink run dir")


def test_backfill_marker_prevents_repeated_scan() -> None:
    root = _reset_runs()
    run_dir = _run_dir(root, "run-backfill-once")
    write_marker(run_dir / "reconciled.marker", "claude")
    runs_dir.reconciled_marker_index_backfill_marker_path(root).unlink(missing_ok=True)
    original_scandir = runs_dir.os.scandir
    count = 0
    count_lock = threading.Lock()

    def counted_scandir(path):
        nonlocal count
        if str(path) == str(root):
            with count_lock:
                count += 1
        return original_scandir(path)

    runs_dir.os.scandir = counted_scandir  # type: ignore
    try:
        assert runs_dir.ensure_reconciled_marker_index_backfilled(root) is True
        assert runs_dir.ensure_reconciled_marker_index_backfilled(root) is False
    finally:
        runs_dir.os.scandir = original_scandir  # type: ignore
    assert count == 1, count
    print("PASS backfill marker prevents repeated scan")


def test_write_marker_indexes_only_runs_root_reconciled_marker() -> None:
    root = _reset_runs()
    run_dir = _run_dir(root, "run-write-marker")
    write_marker(run_dir / "reconciled.marker", "claude")
    outside = Path(tempfile.mkdtemp(prefix="bc-reconciled-outside-"))
    try:
        write_marker(outside / "reconciled.marker", "claude")
    finally:
        shutil.rmtree(outside, ignore_errors=True)
    index = runs_dir.load_reconciled_marker_index(root)
    assert set(index) == {"run-write-marker"}, index
    assert index["run-write-marker"]["ingestion_version"] == current_ingestion_version("claude")
    print("PASS write_marker indexes only runs_root reconciled.marker")


def test_large_recovery_dispatch_repairs_index_without_quadratic_scan() -> None:
    root = _reset_runs()
    indexed_rows: list[dict] = []
    for position in range(2_000):
        run_dir = _run_dir(root, f"indexed-{position}", provider_id="fake")
        marker = run_dir / "reconciled.marker"
        marker.write_text(json.dumps({
            "provider_kind": "claude",
            "ingestion_version": CLAUDE_INGESTION_VERSION,
        }))
        row = runs_dir._reconciled_marker_index_row(
            marker, "claude", CLAUDE_INGESTION_VERSION, root=root,
        )
        assert row is not None
        indexed_rows.append(row)
    from reconciled_marker_index import for_path
    for_path(runs_dir.reconciled_marker_index_path(root)).append_many(indexed_rows)
    runs_dir.reconciled_marker_index_backfill_marker_path(root).write_text(
        json.dumps({"version": 1}), encoding="utf-8",
    )

    for position in range(80):
        run_dir = _run_dir(root, f"repair-{position}", provider_id="fake")
        (run_dir / "reconciled.marker").write_text(json.dumps({
            "provider_kind": "claude",
            "ingestion_version": CLAUDE_INGESTION_VERSION,
        }))
    pending_ids = {f"pending-{position}" for position in range(40)}
    for run_id in pending_ids:
        _run_dir(root, run_id, provider_id="fake")

    class FakeProvider:
        def __init__(self) -> None:
            self.defunct = False
            self.suspended = False
            self.seen: set[str] = set()

        def recover_in_flight(self, *, loop=None, run_id_filter=None):
            del loop
            self.seen = set(run_id_filter or ())
            return [{"run_id": run_id, "alive": False} for run_id in self.seen]

    fake = FakeProvider()
    started = time.perf_counter()
    with mock.patch.object(provider, "get_provider", return_value=fake):
        recovered = provider.recover_all_in_flight()
    elapsed = time.perf_counter() - started
    assert fake.seen == pending_ids
    assert {row["run_id"] for row in recovered} == pending_ids
    assert elapsed < 5.0, elapsed
    print("PASS large recovery dispatch repairs index in bounded time")


def test_targeted_projection_query_ignores_large_history() -> None:
    root = _reset_runs()
    from reconciled_marker_index import for_path
    index = for_path(runs_dir.reconciled_marker_index_path(root))
    rows = []
    for position in range(10_000):
        rows.append({
            "run_id": f"historical-{position}",
            "marker_path": str(root / f"historical-{position}" / "reconciled.marker"),
            "provider_kind": "claude",
            "ingestion_version": CLAUDE_INGESTION_VERSION,
            "marker_size": 1,
            "marker_mtime_ns": position + 1,
            "marker_inode": position + 1,
            "written_at": time.time(),
        })
    index.append_many(rows)
    started = time.perf_counter()
    found = index.load_latest_for(["historical-7", "historical-9000", "missing"])
    elapsed = time.perf_counter() - started
    assert set(found) == {"historical-7", "historical-9000"}
    assert elapsed < 0.2, elapsed
    print("PASS targeted projection query is independent of historical row count")


def test_legacy_jsonl_import_is_complete_and_one_time() -> None:
    root = _reset_runs()
    legacy = runs_dir.reconciled_marker_index_path(root)
    rows = []
    for position in range(2_000):
        run_id = f"legacy-{position}"
        rows.append({
            "run_id": run_id,
            "marker_path": str(root / run_id / "reconciled.marker"),
            "provider_kind": "claude",
            "ingestion_version": CLAUDE_INGESTION_VERSION,
            "marker_size": 1,
            "marker_mtime_ns": position + 1,
            "marker_inode": position + 1,
            "written_at": 1,
        })
    legacy.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    from reconciled_marker_index import ReconciledMarkerIndex
    index = ReconciledMarkerIndex(legacy)
    started = time.perf_counter()
    assert set(index.load_latest_for(["legacy-7", "legacy-1900"])) == {
        "legacy-7", "legacy-1900",
    }
    assert time.perf_counter() - started < 1.0
    with legacy.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({**rows[0], "run_id": "late-legacy"}) + "\n")
    assert index.load_latest_for(["late-legacy"]) == {}
    print("PASS legacy JSONL import is complete and one time")


def test_concurrent_legacy_import_converges_once() -> None:
    root = _reset_runs()
    legacy = runs_dir.reconciled_marker_index_path(root)
    rows = [{
        "run_id": f"concurrent-legacy-{position}",
        "marker_path": str(root / f"concurrent-legacy-{position}" / "reconciled.marker"),
        "provider_kind": "claude",
        "ingestion_version": CLAUDE_INGESTION_VERSION,
        "marker_size": 1,
        "marker_mtime_ns": position + 1,
        "marker_inode": position + 1,
        "written_at": 1,
    } for position in range(500)]
    legacy.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    from reconciled_marker_index import ReconciledMarkerIndex
    barrier = threading.Barrier(8)
    counts: list[int] = []

    def load() -> None:
        barrier.wait()
        counts.append(len(ReconciledMarkerIndex(legacy).load_latest()))

    threads = [threading.Thread(target=load) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert counts == [500] * 8
    print("PASS concurrent legacy import converges once")


def test_reap_removes_projection_row() -> None:
    root = _reset_runs()
    run_dir = _run_dir(root, "run-reap")
    write_marker(run_dir / "reconciled.marker", "claude")
    assert "run-reap" in runs_dir.load_reconciled_marker_index(root)
    assert runs_dir.reap_run_dir(run_dir)
    assert "run-reap" not in runs_dir.load_reconciled_marker_index(root)
    print("PASS reap removes marker projection row")


def test_corrupt_projection_rebuilds_from_authoritative_marker() -> None:
    root = _reset_runs()
    run_dir = _run_dir(root, "run-corrupt")
    write_marker(run_dir / "reconciled.marker", "claude")
    historical = _run_dir(root, "run-non-catalog")
    write_marker(historical / "reconciled.marker", "claude")
    runs_dir.reconciled_marker_index_backfill_marker_path(root).write_text(
        json.dumps({"version": 1}), encoding="utf-8",
    )
    index_path = runs_dir.reconciled_marker_index_path(root).with_suffix(".sqlite3")
    index_path.with_name(index_path.name + "-wal").unlink(missing_ok=True)
    index_path.with_name(index_path.name + "-shm").unlink(missing_ok=True)
    index_path.write_bytes(b"not sqlite")
    from reconciled_marker_index import ReconciledMarkerIndex
    rebuilt = ReconciledMarkerIndex(runs_dir.reconciled_marker_index_path(root))
    assert set(rebuilt.load_latest()) == {"run-corrupt", "run-non-catalog"}
    print("PASS corrupt projection rebuilds all authoritative markers")


def test_busy_projection_is_never_unlinked() -> None:
    root = _reset_runs()
    legacy = runs_dir.reconciled_marker_index_path(root)
    from reconciled_marker_index import ReconciledMarkerIndex
    index = ReconciledMarkerIndex(legacy)
    index.path.write_bytes(b"must-remain")
    busy = sqlite3.OperationalError("database is locked")
    busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
    with mock.patch("reconciled_marker_index.sqlite3.connect", side_effect=busy):
        try:
            index.load_latest()
        except sqlite3.OperationalError as error:
            assert error is busy
        else:
            raise AssertionError("busy database must propagate")
    assert index.path.read_bytes() == b"must-remain"
    print("PASS busy projection is never unlinked")


def test_projection_symlink_is_refused_without_touching_target() -> None:
    root = _reset_runs()
    legacy = runs_dir.reconciled_marker_index_path(root)
    from reconciled_marker_index import ReconciledMarkerIndex
    index = ReconciledMarkerIndex(legacy)
    target = root / "outside-target"
    target.write_bytes(b"untouched")
    index.path.symlink_to(target)
    try:
        index.load_latest()
    except OSError:
        pass
    else:
        raise AssertionError("projection symlink must be refused")
    assert target.read_bytes() == b"untouched"
    print("PASS projection symlink target remains untouched")


def test_unknown_schema_version_fails_without_mutation() -> None:
    root = _reset_runs()
    legacy = runs_dir.reconciled_marker_index_path(root)
    from reconciled_marker_index import ReconciledMarkerIndex
    index = ReconciledMarkerIndex(legacy)
    index.append({
        "run_id": "schema-row",
        "marker_path": str(root / "schema-row" / "reconciled.marker"),
        "provider_kind": "claude",
        "ingestion_version": CLAUDE_INGESTION_VERSION,
        "marker_size": 1,
        "marker_mtime_ns": 1,
        "marker_inode": 1,
        "written_at": 1,
    })
    with sqlite3.connect(index.path) as connection:
        connection.execute(
            "UPDATE metadata SET value=99 WHERE key='schema_version'"
        )
    try:
        index.load_latest()
    except RuntimeError as error:
        assert "schema version: 99" in str(error)
    else:
        raise AssertionError("unknown schema version must fail closed")
    with sqlite3.connect(index.path) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone() == (99,)
        assert connection.execute(
            "SELECT run_id FROM markers"
        ).fetchall() == [("schema-row",)]
    print("PASS unknown schema version fails without mutation")


def test_concurrent_projection_upserts_are_exact() -> None:
    root = _reset_runs()
    from reconciled_marker_index import for_path
    index = for_path(runs_dir.reconciled_marker_index_path(root))
    barrier = threading.Barrier(12)

    def writer(position: int) -> None:
        barrier.wait()
        run_id = "same" if position < 6 else f"distinct-{position}"
        index.append({
            "run_id": run_id,
            "marker_path": str(root / run_id / "reconciled.marker"),
            "provider_kind": "claude",
            "ingestion_version": CLAUDE_INGESTION_VERSION,
            "marker_size": 1,
            "marker_mtime_ns": 1,
            "marker_inode": 1,
            "written_at": 1,
        })

    threads = [threading.Thread(target=writer, args=(position,)) for position in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert set(index.load_latest()) == {"same", *(f"distinct-{p}" for p in range(6, 12))}
    print("PASS concurrent projection upserts are exact")


def main() -> int:
    try:
        test_indexed_current_run_skips_marker_json_and_backend_state()
        test_index_missing_marker_does_not_skip()
        test_index_stale_signature_does_not_skip()
        test_stale_ingestion_version_row_does_not_skip()
        test_backfill_skips_symlink_run_dir()
        test_backfill_marker_prevents_repeated_scan()
        test_write_marker_indexes_only_runs_root_reconciled_marker()
        test_large_recovery_dispatch_repairs_index_without_quadratic_scan()
        test_targeted_projection_query_ignores_large_history()
        test_legacy_jsonl_import_is_complete_and_one_time()
        test_concurrent_legacy_import_converges_once()
        test_reap_removes_projection_row()
        test_corrupt_projection_rebuilds_from_authoritative_marker()
        test_busy_projection_is_never_unlinked()
        test_projection_symlink_is_refused_without_touching_target()
        test_unknown_schema_version_fails_without_mutation()
        test_concurrent_projection_upserts_are_exact()
        return 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
