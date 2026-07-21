from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from requirements_search_supervisor import SEARCH_LIMITS, _failure


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
