import os
import shutil
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc_test_get_req_fail_fast_")

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requirement_context  # noqa: E402


def test_processor_spec_does_not_retry() -> None:
    assert requirement_context.GET_REQUIREMENTS_PROCESSOR_SPEC.retry_attempts == 1
    assert requirement_context.GET_REQUIREMENTS_PROCESSOR_SPEC.provision_timeout < 120


def test_timeout_failure_is_user_facing_and_no_retry() -> None:
    calls = 0
    original_prepare = requirement_context.prepare_requirements_context
    original_run_sync = requirement_context.provisioning.run_sync

    def fake_prepare():
        return {}

    def fake_run_sync(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise TimeoutError("get_requirements_processor provisioned run timed out")

    requirement_context.prepare_requirements_context = fake_prepare
    requirement_context.provisioning.run_sync = fake_run_sync
    try:
        result = requirement_context.get_processed_requirements(query="rate limited glm")
    finally:
        requirement_context.prepare_requirements_context = original_prepare
        requirement_context.provisioning.run_sync = original_run_sync

    assert calls == 1
    assert result["success"] is False
    assert result["requirements"] == []
    assert "query" not in result
    assert "timed out" in result["error"]
    assert "no retry attempted" in result["error"]


def test_rate_limit_failure_is_user_facing_and_no_retry() -> None:
    calls = 0
    original_prepare = requirement_context.prepare_requirements_context
    original_run_sync = requirement_context.provisioning.run_sync

    def fake_prepare():
        return {}

    def fake_run_sync(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("API Error: Request rejected (429) rate limit reached")

    requirement_context.prepare_requirements_context = fake_prepare
    requirement_context.provisioning.run_sync = fake_run_sync
    try:
        result = requirement_context.get_processed_requirements(query="rate limited glm")
    finally:
        requirement_context.prepare_requirements_context = original_prepare
        requirement_context.provisioning.run_sync = original_run_sync

    assert calls == 1
    assert result["success"] is False
    assert result["requirements"] == []
    assert "query" not in result
    assert "rate limit" in result["error"]
    assert "no retry attempted" in result["error"]


if __name__ == "__main__":
    try:
        test_processor_spec_does_not_retry()
        test_timeout_failure_is_user_facing_and_no_retry()
        test_rate_limit_failure_is_user_facing_and_no_retry()
    finally:
        shutil.rmtree(os.environ["BETTER_CLAUDE_HOME"], ignore_errors=True)
