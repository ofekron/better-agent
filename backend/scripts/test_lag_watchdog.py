"""Lag-watchdog dumps the main-thread traceback when the loop heartbeat goes
stale. This is the mechanism that finally makes the recurring multi-second
event-loop lags attributable: the monitor coroutine can only run (and dump)
once the loop is free — i.e. AFTER a synchronous blocker has returned — so a
separate watchdog thread is needed to capture the blocker mid-flight.

Run with:
    cd backend && .venv/bin/python scripts/test_lag_watchdog.py
"""

from __future__ import annotations

import atexit
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-lag-wd-")
atexit.register(lambda: shutil.rmtree(_TMP_HOME, ignore_errors=True))

import main  # noqa: E402
import paths  # noqa: E402
import extension_backend_loader  # noqa: E402


def _heartbeat(age: float = 0.0, process_cpu: float | None = None) -> dict[str, float]:
    return {
        "monotonic": time.monotonic() - age,
        "process_cpu": time.process_time() if process_cpu is None else process_cpu,
    }


def _wait_until(predicate, timeout: float = 10.0, interval: float = 0.02):
    """Poll `predicate` until truthy or the deadline passes; returns the
    last value. Deadline is generous — only the happy path is fast."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(interval)


def _wait_watchdog_cycle_after(t0: float, timeout: float = 5.0) -> bool:
    """Wait until the watchdog thread has completed a poll cycle strictly
    after `t0` (it appends an attribution sample each 0.5s cycle, right
    before reading the heartbeat)."""
    def _cycled() -> bool:
        samples = list(main._LAG_ATTRIBUTION_SAMPLES)
        return bool(samples) and float(samples[-1]["at"]) > t0

    return bool(_wait_until(_cycled, timeout=timeout))


def _dump_text(dump_path) -> str:
    return dump_path.read_text(encoding="utf-8") if dump_path.exists() else ""


def test_incident_window_classification() -> None:
    idle = ["run_until_complete"] * 3
    assert main._classify_lag_incident(
        heartbeat_age=10.0, incident_process_cpu=0.2,
        ready_depth=0, stack_names=idle, stack_frame_ids=[1, 1, 1],
    ) == "blocking I/O or OS deschedule candidate"
    assert main._classify_lag_incident(
        heartbeat_age=10.0, incident_process_cpu=8.0,
        ready_depth=0, stack_names=idle, stack_frame_ids=[1, 1, 1],
    ) == "process CPU/GIL starvation candidate"
    assert main._classify_lag_incident(
        heartbeat_age=3.0, incident_process_cpu=2.0,
        ready_depth=100, stack_names=idle, stack_frame_ids=[1, 1, 1],
    ) == "ready-queue CPU starvation candidate"
    assert main._classify_lag_incident(
        heartbeat_age=3.0, incident_process_cpu=0.1,
        ready_depth=0, stack_names=["resolve"] * 3, stack_frame_ids=[7, 7, 7],
    ) == "blocking stack candidate"
    assert main._classify_lag_incident(
        heartbeat_age=3.0, incident_process_cpu=2.0,
        ready_depth=100, stack_names=["callback"] * 3,
        stack_frame_ids=[1, 2, 3],
    ) == "ready-queue CPU starvation candidate"
    assert main._classify_lag_incident(
        heartbeat_age=3.0, incident_process_cpu=2.0,
        ready_depth=0, stack_names=["callback"] * 3,
        stack_frame_ids=[1, 2, 3],
    ) == "process CPU/GIL starvation candidate"


def test_lag_issue_report_queues_assistant_bug_report() -> None:
    main._report_lag_watchdog_issue(
        label="blocking stack candidate",
        heartbeat_age=4.2,
        dump_path=paths.ba_home() / "logs" / "backend-faulthandler.log",
        evidence="event loop lag evidence heartbeat_age=4.2s",
        stack_names=["sleep", "sleep", "sleep"],
    )
    queued = list((paths.ba_home() / "lag-incidents").glob("*.json"))
    assert len(queued) == 1
    payload = json.loads(queued[0].read_text(encoding="utf-8"))
    assert payload["requirement_ref"].startswith("bug:lag-watchdog:")
    assert payload["summary"] == "Event loop lag: blocking stack candidate ~4.2s"
    assert payload["source"] == "lag_watchdog"
    assert payload["severity"] == "high"
    assert payload["lag_seconds"] == 4.2
    assert payload["stack_names"] == ["sleep", "sleep", "sleep"]


def test_lag_issue_report_spool_full_uses_immutable_indexed_reserve() -> None:
    shutil.rmtree(paths.ba_home() / "lag-incidents", ignore_errors=True)
    queue = main.lag_incident_queue
    original_entries = queue._MAX_TOTAL_ENTRIES
    original_reserve = queue._BACKPRESSURE_RESERVE_ENTRIES
    queue._MAX_TOTAL_ENTRIES = 0
    queue._BACKPRESSURE_RESERVE_ENTRIES = 1
    try:
        main._report_lag_watchdog_issue(
            label="blocking stack candidate",
            heartbeat_age=4.2,
            dump_path=paths.ba_home() / "logs" / "backend-faulthandler.log",
            evidence="event loop lag evidence heartbeat_age=4.2s",
            stack_names=["sleep", "sleep", "sleep"],
        )
    finally:
        queue._MAX_TOTAL_ENTRIES = original_entries
        queue._BACKPRESSURE_RESERVE_ENTRIES = original_reserve
    root = paths.ba_home() / "lag-incidents"
    overflow = list(root.glob("*.overflow"))
    assert len(overflow) == 1
    assert json.loads(overflow[0].read_bytes())["requirement_ref"].startswith("bug:lag-watchdog:")
    with queue._depth_process_lock(root):
        refs = queue._load_overflow_ledger_locked(root)
    assert [entry["name"] for entry in refs] == [overflow[0].name]


def test_lag_report_serialization_boundaries_and_redaction() -> None:
    base = {"summary": "quoted \" evidence", "assistant_message": "\U0001f642", "evidence": "x"}
    exact = main._serialize_lag_report(base)
    original_limit = main._LAG_REPORT_BODY_LIMIT_BYTES
    try:
        main._LAG_REPORT_BODY_LIMIT_BYTES = len(exact)
        assert main._serialize_lag_report(base) == exact
        main._LAG_REPORT_BODY_LIMIT_BYTES = original_limit
        marker_size = len(main._serialize_lag_report({**base, "evidence": main._LAG_REPORT_TRUNCATED}))
        main._LAG_REPORT_BODY_LIMIT_BYTES = marker_size + 7
        clipped = main._serialize_lag_report({**base, "evidence": "\U0001f642" * 100})
        assert len(clipped) <= marker_size + 7
        assert clipped.decode("utf-8")
        assert main._LAG_REPORT_TRUNCATED in clipped.decode("utf-8")
    finally:
        main._LAG_REPORT_BODY_LIMIT_BYTES = original_limit

    evidence = "\n".join([
        "Bearer secret-token",
        "https://example.test/?token=secret-value",
        "access_token=secret-value",
        *(f"line-{index}" for index in range(200)),
    ])
    safe = main._lag_report_evidence(evidence)
    assert "secret-token" not in safe
    assert "secret-value" not in safe
    assert len(safe.splitlines()) == main._LAG_REPORT_MAX_EVIDENCE_LINES
    boundary = main._lag_report_evidence("x" * 500 + " token=boundary-secret " + "y" * 500)
    assert "boundary-secret" not in boundary


def test_lag_report_joint_budget_and_safe_downstream_errors() -> None:
    payload = {
        "summary": "\\\"" * 500,
        "assistant_message": "assistant " * 500,
        "evidence": "evidence " * 10_000,
    }
    encoded = main._serialize_lag_report(payload)
    assert len(encoded) <= main._LAG_REPORT_BODY_LIMIT_BYTES
    decoded = json.loads(encoded)
    assert decoded["assistant_message"] == payload["assistant_message"]
    assert decoded["evidence"].endswith(main._LAG_REPORT_TRUNCATED)
    assert main._safe_extension_error_detail(400, b'{"detail":"evidence too long"}') == "invalid request"
    secret_bodies = [
        b'{"detail":"api_key=sk-secret-value"}',
        b'{"detail":"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.signature"}',
        b'{"detail":"raw-single-line-secret-value"}',
        b'{"detail":"stolen-token-without-bearer-prefix"}',
        b'{"detail":"Bearer stolen-token\\nSet-Cookie: secret=1"}',
    ]
    for body in secret_bodies:
        assert main._safe_extension_error_detail(400, body) == "invalid request"
        assert not any(part in main._safe_extension_error_detail(400, body) for part in body.decode().split())
    assert main._safe_extension_error_detail(401, secret_bodies[0]) == "authentication required"
    assert main._safe_extension_error_detail(429, secret_bodies[1]) == "rate limited"
    assert main._safe_extension_error_detail(500, b'secret internal traceback') == "extension backend failed"


def test_known_unavailable_destination_opens_generation_circuit() -> None:
    original = extension_backend_loader.dispatch_named_core_destination_sync
    warnings = []
    extension_backend_loader.dispatch_named_core_destination_sync = lambda *_args, **_kwargs: (
        extension_backend_loader.NamedCoreDestinationOutcome(
            503,
            b'{"detail":"untrusted and irrelevant"}',
            extension_backend_loader.DestinationAvailability.UNAVAILABLE,
            retry_after=60.0,
        )
    )
    original_warning = main.logger.warning
    main.logger.warning = lambda message, *args, **_kwargs: warnings.append(message % args)
    try:
        outcome = asyncio.run(main._dispatch_lag_watchdog_issue(b"{}"))
    finally:
        extension_backend_loader.dispatch_named_core_destination_sync = original
        main.logger.warning = original_warning
    assert not outcome.acknowledged
    assert not outcome.retryable
    assert outcome.retry_after == 60.0
    assert outcome.destination_unavailable
    assert warnings == [
        "lag-watchdog: assistant board bug report dispatch failed status=503 "
        "category=destination_unavailable detail=extension backend unavailable"
    ]


def test_unavailable_destination_without_retry_after_still_opens_generation_circuit() -> None:
    original = extension_backend_loader.dispatch_named_core_destination_sync
    extension_backend_loader.dispatch_named_core_destination_sync = lambda *_args, **_kwargs: (
        extension_backend_loader.NamedCoreDestinationOutcome(
            503,
            b'{"detail":"untrusted and irrelevant"}',
            extension_backend_loader.DestinationAvailability.UNAVAILABLE,
        )
    )
    try:
        outcome = asyncio.run(main._dispatch_lag_watchdog_issue(b"{}"))
    finally:
        extension_backend_loader.dispatch_named_core_destination_sync = original
    assert not outcome.acknowledged
    assert not outcome.retryable
    assert outcome.retry_after is None
    assert outcome.destination_unavailable


def test_absent_and_no_surface_destinations_open_generation_circuit() -> None:
    for availability in (
        extension_backend_loader.DestinationAvailability.ABSENT,
        extension_backend_loader.DestinationAvailability.NO_SURFACE,
    ):
        outcome = extension_backend_loader.NamedCoreDestinationOutcome(
            404, b'{"detail":"not available"}', availability,
        )
        assert outcome.destination_unavailable

    unknown = extension_backend_loader.NamedCoreDestinationOutcome(
        404,
        b'{"detail":"unknown"}',
        extension_backend_loader.DestinationAvailability.UNKNOWN_DESTINATION,
    )
    assert not unknown.destination_unavailable


def test_response_detail_cannot_open_destination_circuit() -> None:
    original = extension_backend_loader.dispatch_named_core_destination_sync
    extension_backend_loader.dispatch_named_core_destination_sync = lambda *_args, **_kwargs: (
        extension_backend_loader.NamedCoreDestinationOutcome(
            503,
            b'{"detail":"Extension backend is unavailable","retry_after":60}',
            extension_backend_loader.DestinationAvailability.AVAILABLE,
        )
    )
    try:
        outcome = asyncio.run(main._dispatch_lag_watchdog_issue(b"{}"))
    finally:
        extension_backend_loader.dispatch_named_core_destination_sync = original
    assert not outcome.acknowledged
    assert outcome.retryable
    assert not outcome.destination_unavailable


def test_watchdog_dumps_when_heartbeat_stale() -> None:
    # Simulate a loop whose heartbeat has not run for 5s.
    main._LAG_HEARTBEAT[0] = _heartbeat(5.0, time.process_time() - 0.1)
    main._LAG_LAST_DUMP[0] = 0.0

    dump_path = paths.ba_home() / "logs" / "backend-faulthandler.log"
    assert not dump_path.exists()

    reports = []
    main._report_lag_watchdog_issue = lambda **payload: reports.append(payload)
    main._start_lag_watchdog(threshold=0.2, cooldown=0.0)

    # Watchdog polls every 0.5s; the report fires only after the dump file
    # is fully written and closed, so this condition implies a complete dump.
    _wait_until(lambda: dump_path.exists() and reports)

    assert dump_path.exists(), "watchdog did not write a dump for a stale heartbeat"
    content = dump_path.read_text(encoding="utf-8")
    assert "event loop lag evidence" in content
    assert "candidate" in content
    assert "sample_overhead_ms=" in content
    assert "incident_process_cpu_ms=" in content
    assert "incident_process_cpu_ratio=" in content
    assert not re.search(r"sample_age_ms=-", content)
    assert "last_sentinel_callback=" in content
    assert "monitor_task=" in content
    assert " callback=" not in content
    assert " task=" not in content
    overhead = float(re.search(r"sample_overhead_ms=([0-9.]+)", content).group(1))
    assert 100.0 <= overhead < 500.0
    assert content.count("--- sample ") == 3
    assert len(content.splitlines()) > 3, content
    assert reports, "watchdog did not report the lag dump to assistant board"
    assert reports[0]["dump_path"] == dump_path
    assert reports[0]["heartbeat_age"] >= 5.0
    assert reports[0]["label"].endswith("candidate")
    assert "event loop lag evidence" in reports[0]["evidence"]
    assert len(reports[0]["stack_names"]) == 3
    first_count = content.count("=== event loop lag evidence")
    # No re-dump for the SAME stale heartbeat generation: wait until the
    # watchdog has demonstrably completed another poll cycle, then assert
    # the dump count is unchanged.
    assert _wait_watchdog_cycle_after(time.monotonic())
    assert dump_path.read_text(encoding="utf-8").count("=== event loop lag evidence") == first_count

    main._LAG_HEARTBEAT[0] = _heartbeat()
    # Let the watchdog observe the FRESH heartbeat (resets its
    # per-generation dedup) before staging the next stale generation.
    assert _wait_watchdog_cycle_after(time.monotonic())
    main._LAG_HEARTBEAT[0] = _heartbeat(5.0, time.process_time() - 0.1)
    _wait_until(
        lambda: _dump_text(dump_path).count("=== event loop lag evidence") > first_count,
    )
    assert dump_path.read_text(encoding="utf-8").count("=== event loop lag evidence") == first_count + 1
    main._report_lag_watchdog_issue = lambda **_payload: None


def test_real_loop_flood_and_block_have_distinct_evidence() -> None:
    main._report_lag_watchdog_issue = lambda **_payload: None
    dump_path = paths.ba_home() / "logs" / "backend-faulthandler.log"
    dump_path.unlink(missing_ok=True)
    main._LAG_LAST_DUMP[0] = 0.0

    async def flood() -> None:
        loop = asyncio.get_running_loop()
        main._schedule_lag_sentinel(loop)
        counter = [0]

        def short_callback() -> None:
            for _ in range(20):
                counter[0] += 1

        for _ in range(400_000):
            loop.call_soon(short_callback)
        main._LAG_HEARTBEAT[0] = _heartbeat()
        main._start_lag_watchdog(threshold=0.1, cooldown=0.0)
        # Keep the loop alive until the watchdog dumped the asserted
        # evidence; the first await parks until the flood drains, which
        # is the starvation being measured.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            content = _dump_text(dump_path)
            if ("ready-queue CPU starvation candidate" in content
                    and "ready_depth=" in content):
                break
            await asyncio.sleep(0.02)

    asyncio.run(flood())
    flood_content = dump_path.read_text(encoding="utf-8")
    assert "ready-queue CPU starvation candidate" in flood_content
    assert "ready_depth=" in flood_content

    dump_path.unlink(missing_ok=True)
    main._LAG_LAST_DUMP[0] = 0.0

    async def block() -> None:
        main._schedule_lag_sentinel(asyncio.get_running_loop())
        await asyncio.sleep(0)
        main._LAG_HEARTBEAT[0] = _heartbeat()
        main._start_lag_watchdog(threshold=0.1, cooldown=0.0)
        # The synchronous block IS the tested property. The watchdog's poll
        # cadence is a fixed 0.5s (not injectable), and its 3x0.05s stack
        # sampling must land INSIDE the block to capture "in block" frames —
        # so this duration cannot shrink below ~0.7s without flaking.
        time.sleep(1.0)
        # Wait (bounded) until the watchdog wrote the asserted evidence.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            content = _dump_text(dump_path)
            if ("blocking stack candidate" in content
                    and content.count("in block") >= 3):
                break
            await asyncio.sleep(0.02)

    asyncio.run(block())
    block_content = dump_path.read_text(encoding="utf-8")
    assert "blocking stack candidate" in block_content
    assert block_content.count("in block") >= 3


if __name__ == "__main__":
    test_lag_issue_report_queues_assistant_bug_report()
    test_lag_issue_report_spool_full_uses_immutable_indexed_reserve()
    test_lag_report_serialization_boundaries_and_redaction()
    test_lag_report_joint_budget_and_safe_downstream_errors()
    test_known_unavailable_destination_opens_generation_circuit()
    test_unavailable_destination_without_retry_after_still_opens_generation_circuit()
    test_absent_and_no_surface_destinations_open_generation_circuit()
    test_response_detail_cannot_open_destination_circuit()
    test_incident_window_classification()
    test_watchdog_dumps_when_heartbeat_stale()
    test_real_loop_flood_and_block_have_distinct_evidence()
    print("PASS: lag watchdog dumps on stale heartbeat")
