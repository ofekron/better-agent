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
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-lag-wd-")
atexit.register(lambda: shutil.rmtree(_TMP_HOME, ignore_errors=True))

import main  # noqa: E402
import paths  # noqa: E402


def test_lag_issue_report_posts_assistant_bug_report() -> None:
    calls = []

    def invoke_extension_backend_sync(extension_id, path, **kwargs):
        calls.append((extension_id, path, kwargs))
        return 200, b'{"ok":true}'

    original = sys.modules.get("extension_backend_loader")
    sys.modules["extension_backend_loader"] = types.SimpleNamespace(
        invoke_extension_backend_sync=invoke_extension_backend_sync,
    )
    try:
        main._report_lag_watchdog_issue(
            label="blocking stack candidate",
            heartbeat_age=4.2,
            dump_path=paths.ba_home() / "logs" / "backend-faulthandler.log",
            evidence="event loop lag evidence heartbeat_age=4.2s",
            stack_names=["sleep", "sleep", "sleep"],
        )
    finally:
        if original is None:
            sys.modules.pop("extension_backend_loader", None)
        else:
            sys.modules["extension_backend_loader"] = original

    assert len(calls) == 1
    extension_id, path, kwargs = calls[0]
    assert extension_id == "ofek-dev.assistant"
    assert path == "assistant/bug-report"
    assert kwargs["base_url"]
    payload = json.loads(kwargs["body_bytes"].decode("utf-8"))
    assert payload["requirement_ref"].startswith("bug:lag-watchdog:")
    assert payload["summary"] == "Event loop lag: blocking stack candidate ~4.2s"
    assert payload["source"] == "lag_watchdog"
    assert payload["severity"] == "high"
    assert payload["lag_seconds"] == 4.2
    assert payload["stack_names"] == ["sleep", "sleep", "sleep"]


def test_watchdog_dumps_when_heartbeat_stale() -> None:
    # Simulate a loop whose heartbeat has not run for 5s.
    main._LAG_HEARTBEAT[0] = time.monotonic() - 5.0
    main._LAG_LAST_DUMP[0] = 0.0

    dump_path = paths.ba_home() / "logs" / "backend-faulthandler.log"
    assert not dump_path.exists()

    reports = []
    main._report_lag_watchdog_issue = lambda **payload: reports.append(payload)
    main._start_lag_watchdog(threshold=0.2, cooldown=0.0)

    # Watchdog polls every 0.5s; give it a few cycles to notice + dump.
    for _ in range(20):
        if dump_path.exists() and reports:
            break
        time.sleep(0.2)

    assert dump_path.exists(), "watchdog did not write a dump for a stale heartbeat"
    content = dump_path.read_text(encoding="utf-8")
    assert "event loop lag evidence" in content
    assert "candidate" in content
    assert "sample_overhead_ms=" in content
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
    time.sleep(1.1)
    assert dump_path.read_text(encoding="utf-8").count("=== event loop lag evidence") == first_count

    main._LAG_HEARTBEAT[0] = time.monotonic()
    time.sleep(0.6)
    main._LAG_HEARTBEAT[0] = time.monotonic() - 5.0
    for _ in range(20):
        if dump_path.read_text(encoding="utf-8").count("=== event loop lag evidence") > first_count:
            break
        time.sleep(0.2)
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
        main._LAG_HEARTBEAT[0] = time.monotonic()
        main._start_lag_watchdog(threshold=0.1, cooldown=0.0)
        await asyncio.sleep(1.4)

    asyncio.run(flood())
    flood_content = dump_path.read_text(encoding="utf-8")
    assert "heartbeat starvation candidate" in flood_content
    assert "ready_depth=" in flood_content

    dump_path.unlink(missing_ok=True)
    main._LAG_LAST_DUMP[0] = 0.0

    async def block() -> None:
        main._schedule_lag_sentinel(asyncio.get_running_loop())
        await asyncio.sleep(0)
        main._LAG_HEARTBEAT[0] = time.monotonic()
        main._start_lag_watchdog(threshold=0.1, cooldown=0.0)
        time.sleep(1.0)
        await asyncio.sleep(0.2)

    asyncio.run(block())
    block_content = dump_path.read_text(encoding="utf-8")
    assert "blocking stack candidate" in block_content
    assert block_content.count("in block") >= 3


if __name__ == "__main__":
    test_lag_issue_report_posts_assistant_bug_report()
    test_watchdog_dumps_when_heartbeat_stale()
    test_real_loop_flood_and_block_have_distinct_evidence()
    print("PASS: lag watchdog dumps on stale heartbeat")
