#!/usr/bin/env python3
"""Regression lock for durable core extension jobs used by get-requirements.

The backend restarts routinely (auto-restart-on-idle), so extension jobs must be
core-owned durable workflows, not per-extension in-memory registries.
"""
from __future__ import annotations

import asyncio
import json
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

import extension_jobs as jobs  # noqa: E402
import delegation_status_store  # noqa: E402
import provisioning.dispatch as prov_dispatch  # noqa: E402
import requirement_context  # noqa: E402

OWNER = "requirements"
OPERATION = "processed"
TARGET = requirement_context.GET_REQUIREMENTS_PROCESSOR_KEY


def _simulate_restart() -> None:
    """Wipe every in-memory trace, exactly what process death does."""
    jobs._JOBS.clear()
    jobs._COMPLETED_AT.clear()


def _delegation_id(request_id: str) -> str:
    return jobs.delegation_id(OWNER, OPERATION, request_id, TARGET)


def _fire(request_id: str, payload, runner, *, metadata=None):
    return jobs.fire(OWNER, OPERATION, request_id, payload, runner, metadata=metadata)


def _get_or_resume(request_id: str, runner):
    return jobs.get_or_resume(OWNER, OPERATION, request_id, runner)


def _read_record(request_id: str):
    return jobs.read_record(OWNER, OPERATION, request_id)


def _persist_complete(request_id: str, result: dict):
    return jobs.persist_complete(OWNER, OPERATION, request_id, result)


def _persist_running(request_id: str, **fields):
    return jobs.persist_running(OWNER, OPERATION, request_id, **fields)


def _job_path(request_id: str):
    return jobs.job_path(OWNER, OPERATION, request_id)


async def _ok_runner(payload, *, request_id=""):
    return {"success": True, "requirements": [], "count": 0, "echo": payload.get("query")}


async def _failing_runner(payload, *, request_id=""):
    raise RuntimeError("boom")


def test_completed_result_served_from_disk_after_restart() -> None:
    async def scenario():
        task = _fire("job-done", {"query": "q1"}, _ok_runner)
        await task
        _simulate_restart()
        found = _get_or_resume("job-done", _ok_runner)
        assert isinstance(found, dict), f"expected persisted response, got {found!r}"
        assert found["status"] == "complete" and found["ready"] is True
        assert found["result"]["echo"] == "q1"

    asyncio.run(scenario())


def test_failed_result_served_from_disk_after_restart() -> None:
    async def scenario():
        task = _fire("job-fail", {"query": "q2"}, _failing_runner)
        try:
            await task
        except RuntimeError:
            pass
        _simulate_restart()
        found = _get_or_resume("job-fail", _ok_runner)
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

        _fire("job-orphan", {"query": "q3"}, _never_finishes)
        await started.wait()
        # Simulate death mid-flight: record on disk still says running.
        for task in jobs._JOBS.values():
            task.cancel()
        _simulate_restart()
        record = _read_record("job-orphan")
        assert record and record["status"] == "running"

        found = _get_or_resume("job-orphan", _ok_runner)
        assert isinstance(found, asyncio.Task), f"expected resumed task, got {found!r}"
        result = await found
        assert result["echo"] == "q3"
        # Second poll returns the same live task, no double-resume.
        assert _get_or_resume("job-orphan", _ok_runner) is found

    asyncio.run(scenario())


def test_unknown_id_stays_unknown() -> None:
    async def scenario():
        assert _get_or_resume("never-fired", _ok_runner) is None
        try:
            _get_or_resume("../../etc/passwd", _ok_runner)
        except ValueError as exc:
            assert "extension job ids" in str(exc)
        else:
            raise AssertionError("unsafe job id must be rejected")

    asyncio.run(scenario())


def test_reserved_metadata_rejected() -> None:
    async def scenario():
        try:
            _fire("job-bad-metadata", {"query": "q"}, _ok_runner, metadata={"status": "complete"})
        except ValueError as exc:
            assert "reserved keys" in str(exc)
        else:
            raise AssertionError("reserved metadata must be rejected")

    asyncio.run(scenario())


def test_running_progress_served_from_record_after_restart() -> None:
    async def scenario():
        started = asyncio.Event()

        async def _holds(payload, *, request_id=""):
            started.set()
            await asyncio.sleep(3600)
            return {}

        _fire(
            "job-progress",
            {"query": "q-progress"},
            _holds,
            metadata={
                "delegation_id": _delegation_id("job-progress"),
                "phase": "created",
                "message": "created",
            },
        )
        await started.wait()
        running = _persist_running(
            "job-progress",
            phase="queued_for_processor",
            message="Waiting for a requirements processor slot",
        )
        assert running["ready"] is False
        assert running["phase"] == "queued_for_processor"
        assert running["message"] == "Waiting for a requirements processor slot"
        assert running["delegation_id"] == _delegation_id("job-progress")
        assert isinstance(running.get("updated_at"), float)

        for task in jobs._JOBS.values():
            task.cancel()
        _simulate_restart()
        record = _read_record("job-progress")
        assert record and record["status"] == "running"
        response = jobs.response_from_record(record)
        assert response["phase"] == "queued_for_processor"
        assert response["message"] == "Waiting for a requirements processor slot"

    asyncio.run(scenario())


def test_running_progress_does_not_overwrite_terminal_records() -> None:
    async def scenario():
        task = _fire("job-terminal-complete", {"query": "q"}, _ok_runner)
        await task
        complete = _persist_running("job-terminal-complete", phase="late")
        assert complete["status"] == "complete"
        record = _read_record("job-terminal-complete")
        assert record and record["status"] == "complete"
        assert record.get("phase") != "late"

        task = _fire("job-terminal-failed", {"query": "q"}, _failing_runner)
        try:
            await task
        except RuntimeError:
            pass
        failed = _persist_running("job-terminal-failed", phase="late")
        assert failed["status"] == "failed"
        record = _read_record("job-terminal-failed")
        assert record and record["status"] == "failed"
        assert record.get("phase") != "late"

    asyncio.run(scenario())


def test_results_timeout_returns_persisted_running_progress() -> None:
    async def scenario():
        import main

        original_auth = main._internal_authority_is_valid
        original_gate = main._require_builtin_runtime_extension
        original_role = main.extension_store.extension_id_for_role
        started = asyncio.Event()

        async def _holds(payload, *, request_id=""):
            started.set()
            await asyncio.sleep(3600)
            return {}

        try:
            main._internal_authority_is_valid = lambda: True
            main._require_builtin_runtime_extension = lambda _extension_id: None
            main.extension_store.extension_id_for_role = lambda _role: "requirements"
            _fire("job-results-progress", {"query": "q"}, _holds)
            await started.wait()
            _persist_running(
                "job-results-progress",
                phase="queued_for_processor",
                message="Waiting for a requirements processor slot",
            )
            response = await main.internal_get_requirements_results(
                {"id": "job-results-progress", "wait": 0},
                x_internal_token="test",
            )
            assert response["ready"] is False
            assert response["phase"] == "queued_for_processor"
            assert response["message"] == "Waiting for a requirements processor slot"
        finally:
            main._internal_authority_is_valid = original_auth
            main._require_builtin_runtime_extension = original_gate
            main.extension_store.extension_id_for_role = original_role
            for task in jobs._JOBS.values():
                task.cancel()

    asyncio.run(scenario())


def test_phase_persist_failure_does_not_fail_requirements_job() -> None:
    async def scenario():
        import main

        original_persist_running = main.extension_jobs.persist_running
        original_prepare = requirement_context.prepare_requirements_local_read_context
        original_processor = requirement_context._run_requirements_processor
        original_build = requirement_context.build_processed_requirements_response

        def _boom(*args, **kwargs):
            raise OSError("disk unavailable")

        def _prepare():
            return None

        def _processor(**kwargs):
            return {"requirements": []}

        def _build(**kwargs):
            return {"success": True, "requirements": [], "count": 0}

        try:
            main.extension_jobs.persist_running = _boom
            requirement_context.prepare_requirements_local_read_context = _prepare
            requirement_context._run_requirements_processor = _processor
            requirement_context.build_processed_requirements_response = _build
            result = await main._run_processed_requirements_payload(
                {"query": "q", "cwd": "/repo", "cwds": [], "all_projects": False},
                request_id="job-phase-fail",
                queue_admission=False,
            )
            assert result["success"] is True
        finally:
            main.extension_jobs.persist_running = original_persist_running
            requirement_context.prepare_requirements_local_read_context = original_prepare
            requirement_context._run_requirements_processor = original_processor
            requirement_context.build_processed_requirements_response = original_build

    asyncio.run(scenario())


def test_disk_sweep_removes_expired_records() -> None:
    async def scenario():
        task = _fire("job-old", {"query": "q4"}, _ok_runner)
        await task

    asyncio.run(scenario())
    path = _job_path("job-old")
    assert path.exists()
    expired = time.time() - jobs.DISK_RETENTION_SECONDS - 60
    import os

    os.utime(path, (expired, expired))
    jobs._sweep_disk(force=True, owner=OWNER, operation=OPERATION)
    assert not path.exists(), "expired job record must be swept"
    _simulate_restart()
    assert _get_or_resume("job-old", _ok_runner) is None


def test_completed_delegation_recovers_running_async_job() -> None:
    original_get_spec = requirement_context.get_requirements_processor_spec

    class Spec:
        def parse_result(self, text, ctx):
            return {
                "requirements": [{
                    "text": "stage only touched files",
                    "kind": "explicit",
                    "origin": "user_prompt",
                    "polarity": "positive",
                    "strength": "high",
                    "source": "test",
                    "cwd": ctx["cwd"],
                }],
            }

    async def scenario():
        request_id = "job-recover"
        delegation_id = _delegation_id(request_id)

        async def _never_finishes(payload, *, request_id=""):
            await asyncio.sleep(3600)
            return {}

        _fire(
            request_id,
            {"query": "q5", "cwd": "/repo", "cwds": [], "all_projects": False},
            _never_finishes,
            metadata={"delegation_id": delegation_id},
        )
        for task in jobs._JOBS.values():
            task.cancel()
        _simulate_restart()
        record = _read_record(request_id) or {}
        payload = {
            **(record.get("payload") or {}),
            "delegation_id": record.get("delegation_id"),
        }
        assert requirement_context.recover_processed_requirements_from_delegation(
            request_id=request_id,
            payload=payload,
        ) is None

        delegation_status_store.write_status(
            delegation_id,
            status="complete",
            result={
                "success": True,
                "sdk_output": '{"requirements":[{"text":"stage only touched files"}]}',
            },
        )
        recovered = requirement_context.recover_processed_requirements_from_delegation(
            request_id=request_id,
            payload=payload,
        )
        assert recovered and len(recovered["requirements"]) == 1
        final = requirement_context.build_processed_requirements_response(
            query="q5",
            cwd="/repo",
            processed=recovered,
        )
        _persist_complete(request_id, final)
        found = _get_or_resume(request_id, _ok_runner)
        assert isinstance(found, dict)
        assert found["status"] == "complete"
        assert found["result"]["success"] is True

    try:
        requirement_context.get_requirements_processor_spec = lambda: Spec()
        asyncio.run(scenario())
    finally:
        requirement_context.get_requirements_processor_spec = original_get_spec


def test_completed_run_dir_recovers_running_async_job() -> None:
    original_get_spec = requirement_context.get_requirements_processor_spec

    class Spec:
        def parse_result(self, text, ctx):
            return {
                "requirements": [{
                    "text": "recover from complete.json",
                    "kind": "explicit",
                    "origin": "user_prompt",
                    "polarity": "positive",
                    "strength": "high",
                    "source": "test",
                    "cwd": ctx["cwd"],
                }],
            }

    async def scenario():
        request_id = "job-recover-run-dir"
        delegation_id = _delegation_id(request_id)
        run_dir = Path(_TMP_HOME) / "runs" / "run-complete"
        run_dir.mkdir(parents=True)
        (run_dir / "complete.json").write_text(json.dumps({
            "success": True,
            "sdk_output": '{"requirements":[{"text":"recover from complete.json"}]}',
            "session_id": "fork-sid",
        }), encoding="utf-8")

        _fire(
            request_id,
            {"query": "q7", "cwd": "/repo", "cwds": [], "all_projects": False},
            _ok_runner,
            metadata={"delegation_id": delegation_id},
        )
        delegation_status_store.write_status(
            delegation_id,
            status="running",
            provider_run_dir=str(run_dir),
        )
        recovered = requirement_context.recover_processed_requirements_from_delegation(
            request_id=request_id,
            payload={
                "query": "q7",
                "cwd": "/repo",
                "cwds": [],
                "all_projects": False,
                "delegation_id": delegation_id,
            },
        )
        assert recovered and len(recovered["requirements"]) == 1

    try:
        requirement_context.get_requirements_processor_spec = lambda: Spec()
        asyncio.run(scenario())
    finally:
        requirement_context.get_requirements_processor_spec = original_get_spec


def test_completed_run_dir_outside_runs_root_not_recovered() -> None:
    outside = Path(_TMP_HOME) / "outside-run"
    outside.mkdir(parents=True)
    (outside / "complete.json").write_text(json.dumps({
        "success": True,
        "sdk_output": '{"requirements":[{"text":"outside"}]}',
    }), encoding="utf-8")
    assert prov_dispatch.recover_delegation_result("outside-delegation") is None
    delegation_status_store.write_status(
        "outside-delegation",
        status="running",
        provider_run_dir=str(outside),
    )
    assert prov_dispatch.recover_delegation_result("outside-delegation") is None


def test_results_recovery_persists_completed_async_job() -> None:
    original_get_spec = requirement_context.get_requirements_processor_spec

    class Spec:
        def parse_result(self, text, ctx):
            return {
                "requirements": [{
                    "text": "results endpoint recovers",
                    "kind": "explicit",
                    "origin": "user_prompt",
                    "polarity": "positive",
                    "strength": "high",
                    "source": "test",
                    "cwd": ctx["cwd"],
                }],
            }

    async def scenario():
        import main

        request_id = "job-results-recover"
        delegation_id = _delegation_id(request_id)
        _fire(
            request_id,
            {"query": "q8", "cwd": "/repo", "cwds": [], "all_projects": False},
            _ok_runner,
            metadata={"delegation_id": delegation_id},
        )
        _simulate_restart()
        delegation_status_store.write_status(
            delegation_id,
            status="complete",
            result={
                "success": True,
                "sdk_output": '{"requirements":[{"text":"results endpoint recovers"}]}',
            },
        )
        recovered = await main._recover_requirements_async_result(request_id)
        assert isinstance(recovered, dict)
        assert recovered["status"] == "complete"
        assert recovered["result"]["success"] is True
        found = _get_or_resume(request_id, _ok_runner)
        assert isinstance(found, dict)
        assert found["status"] == "complete"

    try:
        requirement_context.get_requirements_processor_spec = lambda: Spec()
        asyncio.run(scenario())
    finally:
        requirement_context.get_requirements_processor_spec = original_get_spec


def test_late_failed_task_does_not_overwrite_recovered_complete() -> None:
    async def scenario():
        release = asyncio.Event()

        async def _fails_after_recovery(payload, *, request_id=""):
            await release.wait()
            raise RuntimeError("boom")

        task = _fire("job-recovered-race", {"query": "q6"}, _fails_after_recovery)
        _persist_complete(
            "job-recovered-race",
            {"success": True, "requirements": [], "count": 0},
        )
        release.set()
        try:
            await task
        except RuntimeError:
            pass
        _simulate_restart()
        found = _get_or_resume("job-recovered-race", _ok_runner)
        assert isinstance(found, dict)
        assert found["status"] == "complete"
        assert found["result"]["success"] is True

    asyncio.run(scenario())


def main() -> int:
    failures = []
    for fn in (
        test_completed_result_served_from_disk_after_restart,
        test_failed_result_served_from_disk_after_restart,
        test_orphaned_running_job_resumes_under_same_id,
        test_unknown_id_stays_unknown,
        test_reserved_metadata_rejected,
        test_running_progress_served_from_record_after_restart,
        test_running_progress_does_not_overwrite_terminal_records,
        test_results_timeout_returns_persisted_running_progress,
        test_phase_persist_failure_does_not_fail_requirements_job,
        test_disk_sweep_removes_expired_records,
        test_completed_delegation_recovers_running_async_job,
        test_completed_run_dir_recovers_running_async_job,
        test_completed_run_dir_outside_runs_root_not_recovered,
        test_results_recovery_persists_completed_async_job,
        test_late_failed_task_does_not_overwrite_recovered_complete,
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
