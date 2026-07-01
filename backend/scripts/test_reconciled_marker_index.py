from __future__ import annotations

import json
import os
import shutil
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
    assert run_dir / "backend_state.json" in reads
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
    assert run_dir / "backend_state.json" in reads
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
    assert run_dir / "backend_state.json" in reads
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


def main() -> int:
    try:
        test_indexed_current_run_skips_marker_json_and_backend_state()
        test_index_missing_marker_does_not_skip()
        test_index_stale_signature_does_not_skip()
        test_stale_ingestion_version_row_does_not_skip()
        test_backfill_skips_symlink_run_dir()
        test_backfill_marker_prevents_repeated_scan()
        test_write_marker_indexes_only_runs_root_reconciled_marker()
        return 0
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
