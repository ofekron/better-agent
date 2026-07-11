#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HOME = tempfile.mkdtemp(prefix="ba-requirements-idempotency-")

import paths  # noqa: E402

paths.engage_test_home(HOME)

import extension_jobs  # noqa: E402
import main  # noqa: E402
import startup_recovery_gate  # noqa: E402


async def _scenario() -> None:
    original = main._run_processed_requirements_payload
    original_validate = main._validate_processed_requirements_body
    original_require = main._require_builtin_runtime_extension
    calls: list[str] = []
    release = asyncio.Event()

    async def runner(payload, *, request_id="", queue_admission=False):
        if queue_admission:
            await startup_recovery_gate.wait_for_recovery_ready(timeout=None)
        calls.append(request_id)
        await release.wait()
        return {"success": True, "requirements": [], "count": 0}

    main._run_processed_requirements_payload = runner
    main._validate_processed_requirements_body = lambda body: {
        key: body.get(key, default)
        for key, default in (("query", ""), ("cwd", ""), ("cwds", []), ("all_projects", False))
    }
    main._require_builtin_runtime_extension = lambda *_args, **_kwargs: None
    try:
        body = {"query": "same", "wait": False, "idempotency_key": "extract-1"}
        first, duplicate = await asyncio.gather(
            main.fire_processed_requirements_for_caller(body, caller_extension="assistant.one"),
            main.fire_processed_requirements_for_caller(body, caller_extension="assistant.one"),
        )
        assert first["id"] == duplicate["id"]
        await asyncio.sleep(0)
        assert calls == [first["id"]]
        assert main._has_restart_blocking_agent_work() is True

        try:
            await main.fire_processed_requirements_for_caller(
                {**body, "query": "changed"}, caller_extension="assistant.one",
            )
        except HTTPException as exc:
            assert exc.status_code == 409
        else:
            raise AssertionError("changed payload reused an idempotency identity")

        other = await main.fire_processed_requirements_for_caller(
            body, caller_extension="assistant.two",
        )
        await asyncio.sleep(0)
        assert other["id"] != first["id"]
        try:
            await main.get_processed_requirements_results_for_caller(
                {"id": first["id"], "wait": 0}, caller_extension="assistant.two",
            )
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("one extension observed another extension's job")

        extension_jobs._JOBS.clear()
        retried = await main.fire_processed_requirements_for_caller(
            body, caller_extension="assistant.one",
        )
        assert retried["id"] == first["id"]
        assert calls == [first["id"], other["id"]]

        release.set()
        await asyncio.sleep(0.05)
    finally:
        main._run_processed_requirements_payload = original
        main._validate_processed_requirements_body = original_validate
        main._require_builtin_runtime_extension = original_require
        startup_recovery_gate.reset_for_tests()


async def _recovery_gate_scenario() -> None:
    original = main._run_processed_requirements_payload
    original_validate = main._validate_processed_requirements_body
    started = asyncio.Event()

    async def runner(payload, *, request_id="", queue_admission=False):
        if queue_admission:
            await startup_recovery_gate.wait_for_recovery_ready(timeout=None)
        started.set()
        return {"success": True, "requirements": [], "count": 0}

    main._run_processed_requirements_payload = runner
    main._validate_processed_requirements_body = lambda body: {
        key: body.get(key, default)
        for key, default in (("query", ""), ("cwd", ""), ("cwds", []), ("all_projects", False))
    }
    startup_recovery_gate.begin_recovery()
    try:
        await main.fire_processed_requirements_for_caller(
            {"query": "gated", "wait": False, "idempotency_key": "extract-gated"},
            caller_extension="assistant.one",
        )
        await asyncio.sleep(0.02)
        assert not started.is_set()
        startup_recovery_gate.mark_recovery_done()
        await asyncio.wait_for(started.wait(), timeout=1)
    finally:
        main._run_processed_requirements_payload = original
        main._validate_processed_requirements_body = original_validate
        startup_recovery_gate.reset_for_tests()


def run() -> None:
    asyncio.run(_scenario())
    extension_jobs._JOBS.clear()
    asyncio.run(_recovery_gate_scenario())


if __name__ == "__main__":
    try:
        run()
        print("ALL PASS")
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
