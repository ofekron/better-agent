from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from types import SimpleNamespace
from pathlib import Path

os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(prefix="ba-recovery-quanta-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import perf
import recovery_priority
import run_recovery
from ingestion_versions import current_ingestion_version


async def test_1206_real_markers_preempt_and_converge() -> None:
    root = run_recovery._runs_root()
    entries = []
    desc = {
        "provider_kind": "claude",
        "ingestion_version": current_ingestion_version("claude"),
    }
    for index in range(1206):
        run_id = f"run-{index:04d}"
        (root / run_id).mkdir(parents=True)
        entries.append((run_id, desc, "fixture", 0))
    ticks: list[float] = []

    async def ticker() -> None:
        while entries:
            ticks.append(time.monotonic())
            await asyncio.sleep(0)

    recovery_priority.interactive_request_started()
    admitted = asyncio.create_task(recovery_priority.admit_recovery_quantum())
    await asyncio.sleep(0.03)
    assert not admitted.done(), "cold marker work must yield to an active request"
    recovery_priority.interactive_request_finished()
    await admitted
    ticker_task = asyncio.create_task(ticker())
    max_quantum = 0.0
    request_latency: list[float] = []
    request_task = None

    async def interactive_request() -> None:
        started = time.monotonic()
        recovery_priority.interactive_request_started()
        await asyncio.sleep(0.01)
        recovery_priority.interactive_request_finished()
        request_latency.append(time.monotonic() - started)

    while entries:
        if len(entries) < 900 and request_task is None:
            request_task = asyncio.create_task(interactive_request())
            await asyncio.sleep(0)
        await recovery_priority.admit_recovery_quantum()
        started = time.monotonic()
        processed, completed, retryable = await asyncio.to_thread(
            run_recovery._write_terminal_marker_quantum,
            entries[:16], None, 5.0,
        )
        max_quantum = max(max_quantum, time.monotonic() - started)
        assert completed == processed
        assert not retryable
        del entries[:processed]
        await asyncio.sleep(0)
    await ticker_task
    if request_task is not None:
        await request_task
    assert ticks and max_quantum < 0.05, (len(ticks), max_quantum)
    assert request_latency and request_latency[0] < 0.05
    assert len(list(root.glob("run-*/reconciled.marker"))) == 1206
    print(
        f"real_io_max_quantum_ms={max_quantum * 1000:.2f} "
        f"interactive_latency_ms={request_latency[0] * 1000:.2f}",
    )


async def test_recovery_cannot_starve_forever() -> None:
    recovery_priority.reset_for_tests()
    original = recovery_priority.MAX_INTERACTIVE_DEFER_SECONDS
    recovery_priority.MAX_INTERACTIVE_DEFER_SECONDS = 0.02
    recovery_priority.interactive_request_started()
    started = time.monotonic()
    await recovery_priority.admit_recovery_quantum()
    elapsed = time.monotonic() - started
    recovery_priority.interactive_request_finished()
    recovery_priority.MAX_INTERACTIVE_DEFER_SECONDS = original
    assert 0.015 <= elapsed < 0.2


def test_marker_path_is_confined_and_atomic() -> None:
    root = run_recovery._runs_root()
    safe = root / "safe-run"
    safe.mkdir(parents=True)
    assert run_recovery._touch_reconciled("safe-run", {"provider_kind": "claude"})
    assert (safe / "reconciled.marker").exists()
    escaped = Path(os.environ["BETTER_AGENT_HOME"]) / "escaped.marker"
    assert not run_recovery._touch_reconciled(
        "../escaped.marker", {"provider_kind": "claude"},
    )
    assert not escaped.exists()
    target = root / "actual-run"
    target.mkdir()
    (root / "linked-run").symlink_to(target, target_is_directory=True)
    assert not run_recovery._touch_reconciled(
        "linked-run", {"provider_kind": "claude"},
    )
    assert not (target / "reconciled.marker").exists()


def test_partial_failure_retries_exact_remaining() -> None:
    root = run_recovery._runs_root()
    desc = {"provider_kind": "claude"}
    entries = []
    for index in range(5):
        run_id = f"partial-{index}"
        (root / run_id).mkdir()
        entries.append((run_id, desc, "fixture", 0))
    original = run_recovery._touch_reconciled
    failed_once = {"partial-2"}

    def injected(run_id, item_desc=None):
        if run_id in failed_once:
            failed_once.remove(run_id)
            return False
        return original(run_id, item_desc)

    run_recovery._touch_reconciled = injected
    processed, completed, retryable = run_recovery._write_terminal_marker_quantum(
        entries, None, 1000,
    )
    run_recovery._touch_reconciled = original
    assert (processed, completed, len(retryable)) == (5, 4, 1)
    processed2, completed2, retryable2 = run_recovery._write_terminal_marker_quantum(
        retryable, None, 1000,
    )
    assert (processed2, completed2, retryable2) == (1, 1, [])
    assert len(list(root.glob("partial-*/reconciled.marker"))) == 5


def test_slow_writer_splits_on_measured_budget() -> None:
    original = run_recovery._mark_reconciled_terminal

    def slow(*args, **kwargs):
        time.sleep(0.004)
        return True

    run_recovery._mark_reconciled_terminal = slow
    entries = [(f"slow-{index}", {}, "fixture", 0) for index in range(10)]
    processed, completed, retryable = run_recovery._write_terminal_marker_quantum(
        entries, None, 5.0,
    )
    run_recovery._mark_reconciled_terminal = original
    assert processed == completed
    assert 1 <= processed <= 2
    assert not retryable
    assert len(entries[processed:]) == 10 - processed


def test_retry_exhaustion_remains_discoverable_next_startup() -> None:
    root = run_recovery._runs_root()
    run_id = "retry-exhausted"
    (root / run_id).mkdir()
    original = run_recovery._touch_reconciled
    run_recovery._touch_reconciled = lambda *_args, **_kwargs: False
    pending = [(run_id, {"provider_kind": "claude"}, "fixture", 0)]
    for _ in range(3):
        _, completed, pending = run_recovery._write_terminal_marker_quantum(
            pending, None, 1000,
        )
        assert completed == 0
    run_recovery._touch_reconciled = original
    assert pending == []
    assert not (root / run_id / "reconciled.marker").exists()
    discovered = {path.name for path in run_recovery.iter_run_dirs()}
    assert run_id in discovered
    with perf._lock:
        assert perf._counts[
            "startup.recovery.terminal_marker.retry_exhausted"
        ]["total"] >= 1


def test_windows_reparse_contract_and_atomic_primitives() -> None:
    assert run_recovery._windows_path_is_reparse(
        SimpleNamespace(st_file_attributes=0x400),
    )
    assert not run_recovery._windows_path_is_reparse(
        SimpleNamespace(st_file_attributes=0),
    )
    from windows_handle_marker import HandleStat, write_atomic_file, write_marker

    class Ops:
        def __init__(self):
            self.stats = {
                "root": HandleStat(1, 1, 0, 0, False),
                "dir": HandleStat(1, 2, 0, 0, False),
                "temp": HandleStat(1, 3, 7, 8, False),
            }
            self.closed = []; self.deleted = []; self.renamed = False
            self.fail = ""; self.dir_stat_calls = 0
        def open_root(self, path): return "root"
        def open_directory_relative(self, root, name): return "dir"
        def create_file_relative(self, directory, name): return "temp"
        def stat(self, handle):
            if handle == "dir":
                self.dir_stat_calls += 1
                if self.fail == "identity" and self.dir_stat_calls >= 2:
                    return HandleStat(1, 99, 0, 0, False)
            return self.stats[handle]
        def write_all(self, handle, data):
            if self.fail == "write": raise OSError("partial write")
        def flush(self, handle): pass
        def rename_relative(self, handle, directory, name):
            if self.fail == "rename": raise OSError("rename")
            self.renamed = True
        def delete_relative(self, directory, name): self.deleted.append(name)
        def close(self, handle): self.closed.append(handle)

    ok = Ops()
    marker = write_marker(ok, Path("C:/runs"), "run", {"x": 1})
    assert marker.file_id == 3 and ok.renamed
    assert ok.closed == ["temp", "dir", "root"] and not ok.deleted
    atomic = Ops()
    written = write_atomic_file(atomic, Path("C:/runs"), "catalog.json", b"{}")
    assert written.file_id == 3 and atomic.renamed
    assert atomic.closed == ["temp", "root"] and not atomic.deleted
    for failure in ("write", "rename", "identity"):
        ops = Ops(); ops.fail = failure
        try:
            write_marker(ops, Path("C:/runs"), "run", {"x": 1})
        except OSError:
            pass
        else:
            raise AssertionError(f"{failure} must fail closed")
        assert ops.closed[-2:] == ["dir", "root"]
        assert len(ops.deleted) == 1
    reparse = Ops(); reparse.stats["dir"] = HandleStat(1, 2, 0, 0, True)
    try:
        write_marker(reparse, Path("C:/runs"), "run", {})
    except OSError:
        pass
    else:
        raise AssertionError("reparse directory must fail closed")


async def main() -> None:
    with perf._lock:
        perf._stats.clear()
        perf._counts.clear()
    await test_1206_real_markers_preempt_and_converge()
    await test_recovery_cannot_starve_forever()
    test_marker_path_is_confined_and_atomic()
    test_partial_failure_retries_exact_remaining()
    test_slow_writer_splits_on_measured_budget()
    test_retry_exhaustion_remains_discoverable_next_startup()
    test_windows_reparse_contract_and_atomic_primitives()
    with perf._lock:
        assert perf._counts["startup.recovery.quantum.preempted"]["total"] >= 1
        assert perf._counts["startup.recovery.quantum.starvation_escape"]["total"] >= 1
    print("PASS recovery priority quanta")


if __name__ == "__main__":
    asyncio.run(main())
