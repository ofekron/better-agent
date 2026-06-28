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
    assert "rate limited" not in result["error"]
    assert "rate limit" not in result["error"]
    assert "unavailable" not in result["error"]
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


def test_rate_limit_detection_precedes_generic_timeout_text() -> None:
    original_run_sync = requirement_context.provisioning.run_sync

    def fake_run_sync(*_args, **_kwargs):
        raise RuntimeError("429 rate limit: provider request timed out while throttled")

    requirement_context.provisioning.run_sync = fake_run_sync
    try:
        result = requirement_context._run_requirements_processor(query="rate limited glm")
    finally:
        requirement_context.provisioning.run_sync = original_run_sync

    assert result["requirements"] == []
    assert "hit a provider rate limit" in result["error"]
    assert "timed out before returning requirements" not in result["error"]


def test_speculative_rate_limit_timeout_text_stays_timeout() -> None:
    # Every speculative hedge — not just "may be" — must stay a timeout. A
    # denylist of hedges is unbounded; the classifier must require a STRONG
    # rate-limit marker, so bare "rate limited" with any hedge is a timeout.
    speculative_hedges = [
        "processor timed out; provider may be rate limited or unavailable",
        "request timed out, provider appears rate limited",
        "timeout - likely rate limited upstream",
        "timed out, seems rate limited",
        "timed out; could be rate limited",
        "timed out, probably rate limited",
        "timed out; possibly hit a rate limit",
    ]
    original_run_sync = requirement_context.provisioning.run_sync
    try:
        for hedge in speculative_hedges:
            def fake_run_sync(*_args, _hedge=hedge, **_kwargs):
                raise RuntimeError(_hedge)

            requirement_context.provisioning.run_sync = fake_run_sync
            result = requirement_context._run_requirements_processor(query="rate limited glm")

            assert result["requirements"] == [], hedge
            assert "timed out before returning requirements" in result["error"], hedge
            assert "hit a provider rate limit" not in result["error"], hedge
    finally:
        requirement_context.provisioning.run_sync = original_run_sync


def test_strong_rate_limit_markers_are_classified_as_rate_limit() -> None:
    # Strong, unambiguous markers must still classify as a confirmed rate limit
    # even when the surrounding text also mentions a timeout.
    strong_markers = [
        "API Error: Request rejected (429)",
        "provider returned rate_limit_exceeded",
        "Rate limit reached, resets 11pm",
        "RESOURCE_EXHAUSTED daily quota",
        "HTTP 429: too many requests; request also timed out",
        "quota exceeded for this project",
    ]
    original_run_sync = requirement_context.provisioning.run_sync
    try:
        for marker in strong_markers:
            def fake_run_sync(*_args, _marker=marker, **_kwargs):
                raise RuntimeError(_marker)

            requirement_context.provisioning.run_sync = fake_run_sync
            result = requirement_context._run_requirements_processor(query="rate limited glm")

            assert result["requirements"] == [], marker
            assert "hit a provider rate limit" in result["error"], marker
            assert "timed out before returning requirements" not in result["error"], marker
    finally:
        requirement_context.provisioning.run_sync = original_run_sync


if __name__ == "__main__":
    try:
        test_processor_spec_does_not_retry()
        test_timeout_failure_is_user_facing_and_no_retry()
        test_rate_limit_failure_is_user_facing_and_no_retry()
        test_rate_limit_detection_precedes_generic_timeout_text()
        test_speculative_rate_limit_timeout_text_stays_timeout()
        test_strong_rate_limit_markers_are_classified_as_rate_limit()
    finally:
        shutil.rmtree(os.environ["BETTER_CLAUDE_HOME"], ignore_errors=True)
