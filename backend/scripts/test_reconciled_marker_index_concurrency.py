from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import _test_home

HOME = _test_home.isolate("bc-test-reconciled-index-concurrency-")
ROOT = Path(HOME) / "runs"
ROOT.mkdir(parents=True, exist_ok=True)

import reconciled_marker_index as index_module  # noqa: E402


def _row(run_id: str, generation: int = 1) -> dict:
    return {
        "run_id": run_id,
        "marker_path": str(ROOT / run_id / "reconciled.marker"),
        "provider_kind": "claude",
        "ingestion_version": 2,
        "marker_size": generation,
        "marker_mtime_ns": generation,
        "marker_inode": generation,
        "written_at": generation,
    }


def _fresh(name: str) -> tuple[Path, index_module.ReconciledMarkerIndex]:
    path = ROOT / f"{name}.jsonl"
    path.unlink(missing_ok=True)
    path.with_suffix(path.suffix + ".lock").unlink(missing_ok=True)
    return path, index_module.ReconciledMarkerIndex(path)


def test_incremental_append_reads_seed_once() -> None:
    path, index = _fresh("linear")
    with path.open("wb") as stream:
        for position in range(8_000):
            stream.write(json.dumps(_row(f"seed-{position}")).encode() + b"\n")
    assert len(index.load_latest()) == 8_000
    initial_offset = index._offset
    for position in range(100):
        assert index.append(_row(f"new-{position}"))
    assert index._offset == path.stat().st_size
    assert initial_offset < index._offset
    assert len(index.load_latest()) == 8_100


def test_partial_malformed_and_duplicate_rows() -> None:
    path, index = _fresh("partial")
    first = json.dumps(_row("first")).encode()
    invalid_rows = [
        {**_row("bad-string"), "marker_size": "bad"},
        {**_row("bad-null"), "marker_mtime_ns": None},
        {**_row("bad-array"), "marker_inode": []},
        {**_row("bad-large"), "marker_inode": 2**80},
    ]
    path.write_bytes(
        first + b"\n" + b"".join(json.dumps(row).encode() + b"\n" for row in invalid_rows)
        + b"{broken"
    )
    assert set(index.load_latest()) == {"first"}
    assert index.append(_row("second"))
    assert not index.append(_row("second"))
    assert set(index.load_latest()) == {"first", "second"}
    for raw in path.read_bytes().splitlines():
        if raw == b"{broken":
            continue
        json.loads(raw)


def test_truncation_and_same_size_replacement_rebuild() -> None:
    path, index = _fresh("replacement")
    original = json.dumps(_row("original"), separators=(",", ":")).encode() + b"\n"
    path.write_bytes(original)
    assert set(index.load_latest()) == {"original"}
    replacement = json.dumps(_row("replaced"), separators=(",", ":")).encode() + b"\n"
    if len(replacement) < len(original):
        replacement = replacement[:-1] + b" " * (len(original) - len(replacement)) + b"\n"
    elif len(replacement) > len(original):
        original = original[:-1] + b" " * (len(replacement) - len(original)) + b"\n"
        path.write_bytes(original)
        index.load_latest()
    path.write_bytes(replacement)
    assert set(index.load_latest()) == {"replaced"}
    path.write_text(json.dumps(_row("truncated")) + "\n")
    assert set(index.load_latest()) == {"truncated"}


def test_threaded_same_key_and_distinct_keys_are_exact() -> None:
    path, index = _fresh("threads")
    barrier = threading.Barrier(20)

    def writer(position: int) -> None:
        barrier.wait()
        index.append(_row("same" if position < 10 else f"distinct-{position}"))

    threads = [threading.Thread(target=writer, args=(position,)) for position in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    rows = [json.loads(raw) for raw in path.read_text().splitlines()]
    assert sum(row["run_id"] == "same" for row in rows) == 1
    assert len(index.load_latest()) == 11


def test_multiprocess_appends_have_no_torn_or_duplicate_rows() -> None:
    path, _index = _fresh("processes")
    code = """
import json,sys
from pathlib import Path
from reconciled_marker_index import ReconciledMarkerIndex
path=Path(sys.argv[1]); run_id=sys.argv[2]
row={"run_id":run_id,"marker_path":str(path.parent/run_id/'reconciled.marker'),"provider_kind":"claude","ingestion_version":2,"marker_size":1,"marker_mtime_ns":1,"marker_inode":1,"written_at":1}
ReconciledMarkerIndex(path).append(row)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(path), f"run-{position % 6}"],
            env=env,
        )
        for position in range(18)
    ]
    assert all(process.wait(timeout=20) == 0 for process in processes)
    rows = [json.loads(raw) for raw in path.read_text().splitlines()]
    assert len(rows) == 6
    assert {row["run_id"] for row in rows} == {f"run-{position}" for position in range(6)}


def main() -> None:
    test_incremental_append_reads_seed_once()
    test_partial_malformed_and_duplicate_rows()
    test_truncation_and_same_size_replacement_rebuild()
    test_threaded_same_key_and_distinct_keys_are_exact()
    test_multiprocess_appends_have_no_torn_or_duplicate_rows()
    print("ALL PASS")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
