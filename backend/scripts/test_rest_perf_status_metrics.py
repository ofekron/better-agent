from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.responses import Response

import _test_home


_tmp = _test_home.isolate("ba-rest-perf-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_installation

_test_installation.activate(Path(_tmp), provider="codex")

import main  # noqa: E402
import recovery_priority  # noqa: E402


ROUTE_TEMPLATE = "/api/sessions/{session_id}"
RAW_PATH = "/api/sessions/private-session-id"


def _request():
    return SimpleNamespace(method="POST", scope={"path": RAW_PATH})


def _route_request(request) -> None:
    request.scope["route"] = SimpleNamespace(path=ROUTE_TEMPLATE)


@pytest.mark.parametrize("status_code", [200, 422])
def test_perf_timing_counts_normal_response_status(status_code: int) -> None:
    request = _request()
    response = Response(status_code=status_code)

    async def call_next(current_request):
        _route_request(current_request)
        return response

    with (
        patch.object(main.perf, "record") as record,
        patch.object(main.perf, "record_count") as record_count,
        patch.object(recovery_priority, "interactive_request_started"),
        patch.object(recovery_priority, "interactive_request_finished"),
    ):
        actual = asyncio.run(main.perf_timing(request, call_next))

    assert actual is response
    record.assert_called_once()
    metric_name, elapsed_ms = record.call_args.args
    assert metric_name == f"rest.POST.{ROUTE_TEMPLATE}"
    assert elapsed_ms >= 0
    record_count.assert_called_once_with(
        f"rest.POST.{ROUTE_TEMPLATE}.status.{status_code}"
    )
    status_metric = record_count.call_args.args[0]
    assert RAW_PATH not in status_metric
    assert "private-session-id" not in status_metric


def test_perf_timing_counts_exception_as_500_and_rethrows_same_error() -> None:
    request = _request()
    expected = RuntimeError("private validation detail")

    async def call_next(current_request):
        _route_request(current_request)
        raise expected

    with (
        patch.object(main.perf, "record") as record,
        patch.object(main.perf, "record_count") as record_count,
        patch.object(recovery_priority, "interactive_request_started"),
        patch.object(recovery_priority, "interactive_request_finished"),
    ):
        with pytest.raises(RuntimeError) as raised:
            asyncio.run(main.perf_timing(request, call_next))

    assert raised.value is expected
    record.assert_called_once()
    record_count.assert_called_once_with(
        f"rest.POST.{ROUTE_TEMPLATE}.status.500"
    )
    status_metric = record_count.call_args.args[0]
    assert str(expected) not in status_metric
    assert RAW_PATH not in status_metric


def test_perf_timing_does_not_classify_cancellation_as_http_500() -> None:
    request = _request()
    expected = asyncio.CancelledError("private cancellation detail")

    async def call_next(current_request):
        _route_request(current_request)
        raise expected

    with (
        patch.object(main.perf, "record") as record,
        patch.object(main.perf, "record_count") as record_count,
        patch.object(recovery_priority, "interactive_request_started"),
        patch.object(recovery_priority, "interactive_request_finished"),
    ):
        with pytest.raises(asyncio.CancelledError) as raised:
            asyncio.run(main.perf_timing(request, call_next))

    assert raised.value is expected
    record.assert_called_once()
    record_count.assert_not_called()
