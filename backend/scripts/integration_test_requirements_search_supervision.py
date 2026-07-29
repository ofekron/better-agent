from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requirements_search_supervisor as supervisor
import requirement_prewarm
from requirements_search_supervisor import (
    SEARCH_LIMITS,
    SearchLimits,
    _communicate_with_posix_limits,
    _failure,
)
from requirements_search_worker import WORKER_ACTION_NAMES, _set_posix_limit


def test_every_processor_route_is_supervised() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    expected = {
        "internal_search_requirements": "unit_rg",
        "internal_requirements_unit_fts": "unit_fts",
        "internal_requirements_unit_vector": "unit_vector",
        "internal_requirements_thread_fts": "thread_fts",
        "internal_requirements_thread_vector": "thread_vector_search",
        "internal_requirements_index_sql": "index_sql",
    }
    for function_name, action in expected.items():
        start = source.index(f"async def {function_name}(")
        end = source.find("\n@app.", start + 1)
        body = source[start:end if end != -1 else len(source)]
        assert "run_supervised_requirements_search" in body
        assert f'"{action}"' in body
        limits = SEARCH_LIMITS[action]
        assert limits.memory_mb > 0
        assert limits.cpu_seconds > 0
        assert limits.wall_seconds > 0
    assert set(SEARCH_LIMITS) == set(WORKER_ACTION_NAMES)


def test_resource_failure_is_clear_and_retryable() -> None:
    result = _failure("unit_vector", "memory", "3072MB")
    assert result["error_code"] == "requirements_search_resource_limit"
    assert result["retryable"] is True
    assert result["retry_strategy"] == "narrow_query"
    assert "Retry with a finer search" in result["error"]
    assert result["lane"] == "unit-vector"
    assert result["resource"] == "memory"


def test_projection_build_failure_does_not_claim_narrowing_will_help() -> None:
    result = _failure("thread_vector_build", "memory", "3072MB")
    assert result["retryable"] is True
    assert result["retry_strategy"] == "resume_projection_build"
    assert "narrow" not in result["error"].lower()


def test_thread_vector_search_failure_does_not_claim_narrowing_will_help() -> None:
    result = _failure("thread_vector_search", "memory", "1024MB")
    assert result["retryable"] is False
    assert "retry_strategy" not in result
    assert "narrow" not in result["error"].lower()


def test_posix_worker_applies_cpu_limit_without_startup_memory_limit() -> None:
    if os.name != "posix":
        return
    worker = (ROOT / "requirements_search_worker.py").read_text(encoding="utf-8")
    assert "RLIMIT_CPU" in worker
    assert "RLIMIT_AS" not in worker


def test_posix_supervisor_isolates_worker_process_group() -> None:
    if os.name != "posix":
        return
    supervisor_source = (ROOT / "requirements_search_supervisor.py").read_text(
        encoding="utf-8"
    )
    assert 'launch["start_new_session"] = True' in supervisor_source


def test_posix_cpu_limit_failure_names_the_exceeded_limit() -> None:
    resource = SimpleNamespace(
        RLIMIT_CPU=0,
        RLIM_INFINITY=9223372036854775807,
        setrlimit=lambda limit, pair: (_ for _ in ()).throw(
            ValueError("current limit exceeds maximum limit")
        ),
    )
    try:
        _set_posix_limit(
            resource,
            resource.RLIMIT_CPU,
            "CPU",
            75,
            76,
            resource.RLIM_INFINITY,
            resource.RLIM_INFINITY,
        )
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("CPU limit setup failure must be re-raised clearly")

    assert "CPU limit" in message
    assert "RLIMIT_CPU" in message
    assert "requested soft=75s hard=76s" in message


def test_posix_memory_limit_is_enforced_after_worker_starts() -> None:
    if os.name != "posix":
        return
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys, time\n"
                "sys.stdin.read()\n"
                "sys.stderr.write('started\\n')\n"
                "sys.stderr.flush()\n"
                "blocks = []\n"
                "for _ in range(128):\n"
                "    blocks.append(bytearray(1024 * 1024))\n"
                "    time.sleep(0.01)\n"
                "time.sleep(5)\n"
            ),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert os.getpgid(process.pid) == process.pid
    output = _communicate_with_posix_limits(
        process,
        b"{}",
        SearchLimits(memory_mb=16, cpu_seconds=20, wall_seconds=10),
        None,
    )

    assert output.exceeded_resource == "memory"
    assert output.exceeded_limit == "16MB"
    assert b"started" in output.stderr


def test_worker_setup_failure_detail_surfaces_through_supervisor() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        worker = Path(tmp) / "worker.py"
        worker.write_text(
            "import sys\n"
            "sys.stderr.write('RuntimeError: failed to apply POSIX CPU limit "
            "(RLIMIT_CPU): requested soft=75s hard=76s; inherited "
            "soft=unlimited hard=unlimited; system error: current limit exceeds "
            "maximum limit\\n')\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
        original_worker = supervisor._WORKER
        supervisor._WORKER = worker
        try:
            result = supervisor.run_supervised_search("unit_vector")
        finally:
            supervisor._WORKER = original_worker

    assert result["error_code"] == "requirements_search_worker_failed"
    assert "CPU limit" in result["error"]
    assert "RLIMIT_CPU" in result["error"]
    assert "requested soft=75s hard=76s" in result["error"]
    assert "Traceback" not in result["error"]


def test_supervised_worker_cancellation_kills_child_and_returns() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        worker = Path(tmp) / "worker.py"
        worker.write_text(
            "import sys, time\nsys.stdin.read()\ntime.sleep(30)\n",
            encoding="utf-8",
        )
        original_worker = supervisor._WORKER
        supervisor._WORKER = worker
        cancel_event = threading.Event()
        result_box = {}

        def run() -> None:
            result_box["result"] = supervisor.run_supervised_search(
                "thread_vector_build",
                _cancel_event=cancel_event,
            )

        thread = threading.Thread(target=run)
        try:
            thread.start()
            cancel_event.set()
            thread.join(timeout=5)
        finally:
            supervisor._WORKER = original_worker
        assert not thread.is_alive()
        assert result_box["result"]["resource"] == "cancelled"
        assert result_box["result"]["retry_strategy"] == "resume_projection_build"


def test_background_projection_shutdown_quiesces_managed_worker() -> None:
    original = supervisor.run_supervised_search
    started = threading.Event()
    stopped = threading.Event()

    def fake_run(action: str, **kwargs):
        assert action == "thread_vector_build"
        cancel_event = kwargs["_cancel_event"]
        started.set()
        assert cancel_event.wait(timeout=5)
        stopped.set()
        return {"success": False, "resource": "cancelled"}

    async def exercise() -> None:
        supervisor.run_supervised_search = fake_run
        try:
            requirement_prewarm.request_thread_vector_projection()
            assert await asyncio.to_thread(started.wait, 5)
            await requirement_prewarm.shutdown_thread_vector_projection()
        finally:
            supervisor.run_supervised_search = original

    asyncio.run(exercise())
    assert stopped.is_set()


def test_windows_worker_uses_job_memory_and_cpu_limits() -> None:
    supervisor = (ROOT / "requirements_search_supervisor.py").read_text(encoding="utf-8")
    assert "CreateJobObjectW" in supervisor
    assert "PerJobUserTimeLimit" in supervisor
    assert "JobMemoryLimit" in supervisor


if __name__ == "__main__":
    for name, test in sorted(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
            print("PASS", name)
