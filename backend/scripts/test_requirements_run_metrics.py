#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="better-agent-requirements-metrics-")
os.environ["BETTER_AGENT_HOME"] = HOME
os.environ["BETTER_AGENT_TEST_MODE"] = "1"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _event(*blocks: dict) -> dict:
    return {
        "type": "agent_message",
        "data": {"message": {"content": list(blocks)}},
    }


def _result(tool_use_id: str, payload: dict) -> dict:
    return _event({
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": json.dumps(payload),
    })


def test_processor_attempt_metrics_are_attributed_to_one_run() -> None:
    import requirements_run_metrics as metrics

    events = [
        _event(
            {"type": "tool_use", "id": "rg-1", "name": "mcp__requirements__search_requirement_units_rg", "input": {}},
            {"type": "tool_use", "id": "fts-1", "name": "search_requirement_units_fts", "input": {}},
            {"type": "tool_use", "id": "vec-1", "name": "search_requirement_units_vector", "input": {"top_k": 25, "min_score": 0.0}},
        ),
        _result("rg-1", {"success": True, "count": 3}),
        _result("fts-1", {"success": True, "count": 2}),
        _result("vec-1", {"success": True, "count": 25, "truncated": True}),
        _event({"type": "tool_use", "id": "vec-2", "name": "search_requirement_units_vector", "input": {"top_k": 200, "min_score": 0.0}}),
        _result("vec-2", {"success": True, "count": 41, "truncated": False}),
    ]
    observed = metrics.observe_processor_attempt(
        provider_id="codex",
        model="gpt-test",
        timings_ms={
            "resolve_config_ms": 4.0,
            "ensure_lifecycle_ms": 12.0,
            "build_prompts_ms": 2.0,
            "dispatch_runner_enqueue_to_first_event_ms": 8.0,
            "dispatch_runner_enqueue_to_first_tool_ms": 10.0,
            "total_ms": 120.0,
        },
        dispatch_result={"events": events},
    )

    assert observed["provider"]["prepare_ms"] == 14.0
    assert observed["tools"] == {
        "observed_names": [
            "mcp__requirements__search_requirement_units_rg",
            "search_requirement_units_fts",
            "search_requirement_units_vector",
        ],
        "first_allowed_search_tool": "search_requirement_units_rg",
        "first_allowed_search_latency_ms": 10.0,
        "off_profile_names": [],
        "policy_valid": True,
    }
    assert observed["search"]["rounds"] == 2
    assert observed["search"]["vector_rounds"] == 2
    assert observed["search"]["vector_params"] == [
        {"top_k": 25, "min_score": 0.0},
        {"top_k": 200, "min_score": 0.0},
    ]
    assert observed["search"]["calls_by_tool"] == {
        "search_requirement_units_fts": 1,
        "search_requirement_units_rg": 1,
        "search_requirement_units_vector": 2,
    }
    assert observed["search"]["result_counts_by_tool"] == {
        "search_requirement_units_fts": [2],
        "search_requirement_units_rg": [3],
        "search_requirement_units_vector": [25, 41],
    }
    assert observed["search"]["error_count"] == 0
    assert observed["search"]["truncated_count"] == 1


def test_off_profile_first_tool_never_gets_false_search_latency() -> None:
    import requirements_run_metrics as metrics

    observed = metrics.observe_processor_attempt(
        provider_id="claude",
        model="test",
        timings_ms={"dispatch_runner_enqueue_to_first_tool_ms": 5.0},
        dispatch_result={"events": [
            _event({"type": "tool_use", "id": "read", "name": "Read", "input": {}}),
            _event({"type": "tool_use", "id": "rg", "name": "search_requirement_units_rg", "input": {}}),
            _result("rg", {"success": True, "count": 1}),
        ]},
    )

    assert observed["tools"]["off_profile_names"] == ["Read"]
    assert observed["tools"]["policy_valid"] is False
    assert observed["tools"]["first_allowed_search_tool"] == "search_requirement_units_rg"
    assert observed["tools"]["first_allowed_search_latency_ms"] is None


def test_dispatch_to_runner_blockage_is_measured_without_provider_misattribution() -> None:
    import requirements_run_metrics as metrics

    current = metrics.new_metrics(created_at=10.0)
    current = metrics.apply_milestone(
        current,
        "processor_attempt_started",
        {"attempt": 1},
        observed_at=20.0,
        created_at=10.0,
    )
    current = metrics.apply_milestone(
        current,
        "dispatch_started",
        {},
        observed_at=30.0,
        created_at=10.0,
    )
    current = metrics.apply_milestone(
        current,
        "runner_started",
        {},
        observed_at=854.5,
        created_at=10.0,
    )

    assert current["provider"]["dispatch_to_runner_ms"] == 824_500.0
    assert current["provider"]["runner_to_native_session_ms"] is None
    current = metrics.apply_milestone(
        current,
        "native_session_started",
        {},
        observed_at=855.5,
        created_at=10.0,
    )
    assert current["provider"]["runner_to_native_session_ms"] == 1_000.0


def test_output_report_counts_deterministic_dedup_and_provenance() -> None:
    import requirement_context

    row = {
        "text": "Keep one owner.",
        "kind": "explicit",
        "origin": "user_prompt",
        "polarity": "positive",
        "strength": "high",
        "source": "user statement",
        "evidence": {"unit_source_key": "unit-1"},
        "cwd": "/repo",
    }
    text, report = requirement_context.analyze_processor_output(json.dumps({
        "requirements": [row, dict(row)],
    }))

    assert json.loads(text)["requirements"] == [row]
    assert report == {
        "raw_count": 2,
        "final_count": 1,
        "dedup_removed": 1,
        "schema_valid": True,
        "provenance_valid": True,
        "identified_evidence_count": 2,
        "unit_evidence_count": 2,
        "transcript_evidence_count": 0,
    }


def test_malformed_output_is_observed_without_becoming_an_empty_success() -> None:
    import requirement_context

    text, report = requirement_context.analyze_processor_output("not json")
    assert text == "not json"
    assert report["raw_count"] is None
    assert report["final_count"] is None
    assert report["schema_valid"] is False
    assert report["provenance_valid"] is None


def test_projection_updates_are_atomic_under_concurrency() -> None:
    import extension_jobs

    job_id = "atomic-metrics"

    async def waiting_runner(*_args, **_kwargs) -> dict:
        await asyncio.sleep(60)
        return {}

    async def create_job() -> None:
        original_ensure_admitted = extension_jobs._ensure_admitted
        extension_jobs._ensure_admitted = lambda: None
        task = extension_jobs.fire(
            "requirements",
            "processed",
            job_id,
            {"query": "q"},
            waiting_runner,
            metadata={"requirements_metrics": {"count": 0}},
        )
        try:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            extension_jobs._ensure_admitted = original_ensure_admitted

    asyncio.run(create_job())

    def increment() -> None:
        extension_jobs.persist_projection(
            "requirements",
            "processed",
            job_id,
            "requirements_metrics",
            lambda current: {"count": int(current.get("count") or 0) + 1},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _index: increment(), range(50)))

    record = extension_jobs.read_record_strict("requirements", "processed", job_id)
    assert record is not None
    assert record["requirements_metrics"] == {"count": 50}


def test_metrics_persistence_failure_never_fails_the_processor_job() -> None:
    import extension_jobs
    import requirements_run_metrics as metrics

    original_read = extension_jobs.read_record_strict

    def fail_read(*_args, **_kwargs):
        raise RuntimeError("corrupt metrics record")

    extension_jobs.read_record_strict = fail_read
    try:
        metrics.record_output("request-1", {"schema_valid": True})
    finally:
        extension_jobs.read_record_strict = original_read


def test_provisioned_result_carries_timings() -> None:
    from provisioning.config import ProvisionedConfig
    from provisioning.manager import ProvisionedResult

    result = ProvisionedResult(
        text="{}",
        value={},
        config=ProvisionedConfig(
            cwd="/repo",
            model="test",
            provider_id="codex",
            reasoning_effort="",
            run_mode="fork",
            dispatch="http",
            on_no_fork="error",
            node_id="primary",
            backend_url="http://127.0.0.1",
            internal_token="token",
            provisioned_session_id=None,
            caller_session_id=None,
            worker_description="requirements",
        ),
        base_session_id="base",
        caller_session_id="caller",
        dispatch_result={},
        timings_ms={"total_ms": 3.0},
    )
    assert result.timings_ms == {"total_ms": 3.0}


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
