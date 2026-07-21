from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requirements_search_supervisor as supervisor
from requirements_search_supervisor import SEARCH_LIMITS, _failure
from requirements_search_worker import _set_posix_limit


def test_four_processor_lanes_are_supervised() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    expected = {
        "internal_search_requirements": "unit_rg",
        "internal_requirements_unit_fts": "unit_fts",
        "internal_requirements_unit_vector": "unit_vector",
        "internal_requirements_index_sql": "index_sql",
    }
    for function_name, action in expected.items():
        start = source.index(f"async def {function_name}(")
        end = source.find("\n@app.", start + 1)
        body = source[start:end if end != -1 else len(source)]
        assert "run_supervised_search" in body
        assert f'action="{action}"' in body
        limits = SEARCH_LIMITS[action]
        assert limits.memory_mb > 0
        assert limits.cpu_seconds > 0
        assert limits.wall_seconds > 0


def test_resource_failure_is_clear_and_retryable() -> None:
    result = _failure("unit_vector", "memory", "3072MB")
    assert result["error_code"] == "requirements_search_resource_limit"
    assert result["retryable"] is True
    assert result["retry_strategy"] == "narrow_query"
    assert "Retry with a finer search" in result["error"]
    assert result["lane"] == "unit-vector"
    assert result["resource"] == "memory"


def test_posix_worker_applies_memory_and_cpu_limits() -> None:
    if os.name != "posix":
        return
    worker = (ROOT / "requirements_search_worker.py").read_text(encoding="utf-8")
    assert "RLIMIT_AS" in worker
    assert "RLIMIT_CPU" in worker


def test_posix_memory_limit_failure_names_the_exceeded_limit() -> None:
    resource = SimpleNamespace(
        RLIMIT_AS=9,
        RLIM_INFINITY=9223372036854775807,
        setrlimit=lambda limit, pair: (_ for _ in ()).throw(
            ValueError("current limit exceeds maximum limit")
        ),
    )
    try:
        _set_posix_limit(
            resource,
            resource.RLIMIT_AS,
            "memory",
            "bytes",
            3072 * 1024 * 1024,
            3072 * 1024 * 1024,
            resource.RLIM_INFINITY,
            resource.RLIM_INFINITY,
        )
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("memory limit setup failure must be re-raised clearly")

    assert "memory limit" in message
    assert "RLIMIT_AS" in message
    assert "requested soft=3072MB hard=3072MB" in message
    assert "inherited soft=unlimited hard=unlimited" in message
    assert "current limit exceeds maximum limit" in message


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
            "seconds",
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


def test_worker_setup_failure_detail_surfaces_through_supervisor() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        worker = Path(tmp) / "worker.py"
        worker.write_text(
            "import sys\n"
            "sys.stderr.write('RuntimeError: failed to apply POSIX memory limit "
            "(RLIMIT_AS): requested soft=3072MB hard=3072MB; inherited "
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
    assert "memory limit" in result["error"]
    assert "RLIMIT_AS" in result["error"]
    assert "requested soft=3072MB hard=3072MB" in result["error"]
    assert "Traceback" not in result["error"]


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
