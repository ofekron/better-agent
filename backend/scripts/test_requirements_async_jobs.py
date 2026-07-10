#!/usr/bin/env python3
"""Regression lock for durable async get-requirements jobs.

The backend restarts routinely (auto-restart-on-idle), and the old in-memory
_REQUIREMENTS_ASYNC_JOBS registry in main.py turned every in-flight lookup into
"unknown id" after a restart. These tests exercise the disk-backed registry:
records survive a simulated process death (in-memory registry wiped), finished
results are served from disk, orphaned running jobs resume under the same id,
and GC sweeps expired records. The pre-fix behavior (registry in main.py only)
has no disk records at all, so every post-"restart" poll here would return
None/unknown — the resume and disk-read assertions fail on that code by
construction.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP_HOME = tempfile.mkdtemp(prefix="ba-req-jobs-test-")

import paths  # noqa: E402

paths.engage_test_home(_TMP_HOME)

import requirements_async_jobs as jobs  # noqa: E402


def _simulate_restart() -> None:
    """Wipe every in-memory trace, exactly what process death does."""
    jobs._JOBS.clear()
    jobs._COMPLETED_AT.clear()


async def _ok_runner(payload, *, request_id=""):
    return {"success": True, "requirements": [], "count": 0, "echo": payload.get("query")}


async def _failing_runner(payload, *, request_id=""):
    raise RuntimeError("boom")


def test_completed_result_served_from_disk_after_restart() -> None:
    async def scenario():
        task = jobs.fire("job-done", {"query": "q1"}, _ok_runner)
        await task
        _simulate_restart()
        found = jobs.get_or_resume("job-done", _ok_runner)
        assert isinstance(found, dict), f"expected persisted response, got {found!r}"
        assert found["status"] == "complete" and found["ready"] is True
        assert found["result"]["echo"] == "q1"

    asyncio.run(scenario())


def test_failed_result_served_from_disk_after_restart() -> None:
    async def scenario():
        task = jobs.fire("job-fail", {"query": "q2"}, _failing_runner)
        try:
            await task
        except RuntimeError:
            pass
        _simulate_restart()
        found = jobs.get_or_resume("job-fail", _ok_runner)
        assert isinstance(found, dict)
        assert found["status"] == "failed" and found["success"] is False
        assert "boom" in found["error"]

    asyncio.run(scenario())


def test_orphaned_running_job_resumes_under_same_id() -> None:
    async def scenario():
        started = asyncio.Event()

        async def _never_finishes(payload, *, request_id=""):
            started.set()
            await asyncio.sleep(3600)
            return {}

        jobs.fire("job-orphan", {"query": "q3"}, _never_finishes)
        await started.wait()
        # Simulate death mid-flight: record on disk still says running.
        for task in jobs._JOBS.values():
            task.cancel()
        _simulate_restart()
        record = jobs._read_record("job-orphan")
        assert record and record["status"] == "running"

        found = jobs.get_or_resume("job-orphan", _ok_runner)
        assert isinstance(found, asyncio.Task), f"expected resumed task, got {found!r}"
        result = await found
        assert result["echo"] == "q3"
        # Second poll returns the same live task, no double-resume.
        assert jobs.get_or_resume("job-orphan", _ok_runner) is found

    asyncio.run(scenario())


def test_unknown_id_stays_unknown() -> None:
    async def scenario():
        assert jobs.get_or_resume("never-fired", _ok_runner) is None
        assert jobs.get_or_resume("../../etc/passwd", _ok_runner) is None

    asyncio.run(scenario())


def test_disk_sweep_removes_expired_records() -> None:
    async def scenario():
        task = jobs.fire("job-old", {"query": "q4"}, _ok_runner)
        await task

    asyncio.run(scenario())
    path = jobs.job_path("job-old")
    assert path.exists()
    expired = time.time() - jobs.DISK_RETENTION_SECONDS - 60
    import os

    os.utime(path, (expired, expired))
    jobs._sweep_disk(force=True)
    assert not path.exists(), "expired job record must be swept"
    _simulate_restart()
    assert jobs.get_or_resume("job-old", _ok_runner) is None


def main() -> int:
    failures = []
    for fn in (
        test_completed_result_served_from_disk_after_restart,
        test_failed_result_served_from_disk_after_restart,
        test_orphaned_running_job_resumes_under_same_id,
        test_unknown_id_stays_unknown,
        test_disk_sweep_removes_expired_records,
    ):
        print(f"--- {fn.__name__} ---")
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            failures.append(f"{fn.__name__}: {exc!r}")
    import shutil

    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
