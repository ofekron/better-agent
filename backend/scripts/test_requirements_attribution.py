#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="better-agent-requirements-attribution-")
os.environ["BETTER_AGENT_HOME"] = HOME
os.environ["BETTER_AGENT_TEST_MODE"] = "1"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_capability_boundary_binds_redacted_attribution() -> None:
    import capability_api
    import requirements_query_runner as runner

    captured: list[runner.RequirementsQueryAttribution] = []

    async def handler(_payload) -> dict[str, bool]:
        captured.append(runner._QUERY_ATTRIBUTION.get())
        return {"ok": True}

    key = ("requirements", "processed")
    original_action = capability_api._ACTIONS.get(key)
    original_require_grant = capability_api._require_grant
    original_generation = capability_api._extension_generation
    capability_api._ACTIONS[key] = capability_api._Action(
        schema=capability_api._RequirementsQueryPayload,
        handler=handler,
    )
    capability_api._require_grant = lambda *_args: "example.requirements-caller"
    capability_api._extension_generation = lambda _extension_id: "generation-redacted"
    secret_query = "private user requirement phrase never log me"
    secret_cwd = "/private/customer/project"
    try:
        result = asyncio.run(capability_api._invoke(
            capability_api.InvokeCapabilityRequest(
                capability="requirements",
                action="processed",
                payload={"query": secret_query, "cwd": secret_cwd},
            ),
            "secret-token-never-log",
        ))
    finally:
        capability_api._require_grant = original_require_grant
        capability_api._extension_generation = original_generation
        if original_action is None:
            capability_api._ACTIONS.pop(key, None)
        else:
            capability_api._ACTIONS[key] = original_action

    assert result == {"ok": True}
    assert len(captured) == 1
    attribution = captured[0]
    assert attribution.caller_extension == "example.requirements-caller"
    assert attribution.action == "processed"
    assert attribution.tool == "requirements.processed"
    assert attribution.extension_generation == "generation-redacted"
    assert attribution.session_id == "unknown"
    assert attribution.run_id == "unknown"
    assert len(attribution.query_scope_hash) == 16
    rendered = repr(attribution)
    assert secret_query not in rendered
    assert secret_cwd not in rendered
    assert "secret-token-never-log" not in rendered


def test_processor_logs_attribution_and_permit_metrics_without_query_text() -> None:
    import requirements_query_runner as runner

    capture = _Capture()
    runner.logger.addHandler(capture)
    runner.logger.setLevel(logging.INFO)
    secret_query = "sensitive requirement body"
    secret_cwd = "/sensitive/worktree"
    token = runner.bind_requirements_attribution(
        request_id="request-123",
        caller_extension="example.caller",
        action="processed",
        tool="requirements.processed",
        payload={"query": secret_query, "cwd": secret_cwd},
        extension_generation="generation-123",
    )
    try:
        result = asyncio.run(runner.run_requirements_processor_query(
            "requirements.processed.processor.instrumented",
            lambda: "ok",
            executor=runner.REQUIREMENTS_PROCESSOR_EXECUTOR,
            admission_timeout_seconds=1.0,
        ))
    finally:
        runner.reset_requirements_attribution(token)
        runner.logger.removeHandler(capture)

    assert result == "ok"
    text = "\n".join(capture.messages)
    assert "event=admission outcome=queued" in text
    assert "event=admitted outcome=running" in text
    assert "event=completion outcome=success" in text
    assert "caller_extension=example.caller" in text
    assert "action=processed tool=requirements.processed" in text
    assert "extension_generation=generation-123" in text
    assert "queue_depth=" in text
    assert "active_permits=" in text
    assert "available_permits=" in text
    assert "oldest_queue_age_ms=" in text
    assert secret_query not in text
    assert secret_cwd not in text


def test_query_scope_hmac_correlates_in_process_without_dictionary_hash_leak() -> None:
    import requirements_query_runner as runner

    payload = {
        "query": "short low entropy query",
        "cwd": "/private/project",
        "cwds": ["/private/other"],
        "all_projects": False,
    }

    def bind_once() -> runner.RequirementsQueryAttribution:
        token = runner.bind_requirements_attribution(
            request_id="scope-correlation",
            caller_extension="example.caller",
            action="processed",
            tool="requirements.processed",
            payload=payload,
        )
        try:
            return runner._QUERY_ATTRIBUTION.get()
        finally:
            runner.reset_requirements_attribution(token)

    first = bind_once()
    second = bind_once()
    assert first.query_scope_hash == second.query_scope_hash
    scope = {
        "caller_extension": "example.caller",
        "query": payload["query"],
        "cwd": payload["cwd"],
        "cwds": payload["cwds"],
        "all_projects": False,
    }
    candidate = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    plain_sha = hashlib.sha256(candidate.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    assert first.query_scope_hash != plain_sha
    rendered = repr(first)
    assert payload["query"] not in rendered
    assert payload["cwd"] not in rendered
    assert payload["cwds"][0] not in rendered
    assert runner._QUERY_SCOPE_HMAC_KEY.hex() not in rendered


def test_waiter_cancellation_removes_queue_depth_without_releasing_permit() -> None:
    import requirements_query_runner as runner

    hold = threading.Event()
    started = threading.Event()
    started_count = 0
    count_lock = threading.Lock()

    def blocker() -> str:
        nonlocal started_count
        with count_lock:
            started_count += 1
            if started_count == runner.PROCESSOR_CAPACITY:
                started.set()
        hold.wait(timeout=5)
        return "released"

    capture = _Capture()
    runner.logger.addHandler(capture)
    runner.logger.setLevel(logging.INFO)

    async def scenario() -> None:
        blockers = [
            asyncio.create_task(runner.run_requirements_processor_query(
                f"requirements.instrumented.blocker.{index}",
                blocker,
                executor=runner.REQUIREMENTS_PROCESSOR_EXECUTOR,
                admission_timeout_seconds=1.0,
            ))
            for index in range(runner.PROCESSOR_CAPACITY)
        ]
        await asyncio.wait_for(asyncio.to_thread(started.wait, 2), timeout=3)
        token = runner.bind_requirements_attribution(
            request_id="cancelled-waiter",
            caller_extension="example.caller",
            action="processed",
            tool="requirements.processed",
            payload={"query": "never rendered"},
        )
        try:
            waiter = asyncio.create_task(runner.run_requirements_processor_query(
                "requirements.instrumented.cancelled",
                lambda: "unexpected",
                executor=runner.REQUIREMENTS_PROCESSOR_EXECUTOR,
                admission_timeout_seconds=2.0,
            ))
        finally:
            runner.reset_requirements_attribution(token)
        await asyncio.sleep(0.03)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        state = runner._admission_state()
        assert state["queue_depth"] == 0
        assert state["active_permits"] == runner.PROCESSOR_CAPACITY
        hold.set()
        await asyncio.gather(*blockers)

    try:
        asyncio.run(scenario())
    finally:
        hold.set()
        runner.logger.removeHandler(capture)

    text = "\n".join(capture.messages)
    assert "request_id=cancelled-waiter" in text
    assert "event=cancellation outcome=admission_cancelled" in text
    assert "query_scope_hash=" in text
    assert "never rendered" not in text


def _run() -> int:
    failures: list[str] = []
    try:
        for name, fn in sorted(globals().items()):
            if not name.startswith("test_") or not callable(fn):
                continue
            try:
                fn()
                print("PASS", name)
            except Exception as exc:
                failures.append(f"{name}: {exc}")
                print("FAIL", name, repr(exc))
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
    if failures:
        print("\n".join(failures))
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run())
