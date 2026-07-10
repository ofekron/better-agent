#!/usr/bin/env python3
"""Regression lock for provisioned-dispatch HTTP resilience.

The get-requirements processor dispatch previously retried only
httpx.TimeoutException/TransportError; an httpx.HTTPStatusError from
raise_for_status (e.g. a transient 503 while the backend restarts) propagated
un-retried and failed the whole lookup. These tests pin: 5xx retries within the
attempt budget, 4xx (including 429 — owned by the rate-limit message path)
fails fast on the first attempt.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

import provisioning.dispatch as dispatch_mod  # noqa: E402
from provisioning.spec import ProvisionedSessionSpec  # noqa: E402


def _spec() -> ProvisionedSessionSpec:
    class _TestSpec(ProvisionedSessionSpec):
        key = "test_processor"
        env_prefix = "TEST"
        retry_attempts = 3
        retry_backoff = (0.0, 0.0)

    return _TestSpec()


class _Cfg:
    dispatch = "http"
    internal_token = "token"
    backend_url = "http://127.0.0.1:1"
    worker_description = "test"
    model = ""
    cwd = "/tmp"
    run_mode = "fork"


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://test/api/internal/ask-fork")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError(f"status {code}", request=request, response=response)


def _run_dispatch(responses: list) -> tuple[dict | Exception, int]:
    calls = {"n": 0}

    async def fake_post(cfg, payload, *, timeout):
        item = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    original = dispatch_mod._post_ask_fork
    dispatch_mod._post_ask_fork = fake_post
    try:
        outcome = asyncio.run(
            dispatch_mod._dispatch_http(
                _spec(),
                _Cfg(),
                base_session_id="base",
                caller_session_id="caller",
                instructions="do",
                provision_prompt="",
            )
        )
        return outcome, calls["n"]
    except Exception as exc:  # noqa: BLE001
        return exc, calls["n"]
    finally:
        dispatch_mod._post_ask_fork = original


def test_5xx_retries_then_succeeds() -> None:
    outcome, attempts = _run_dispatch(
        [_status_error(503), _status_error(529), {"success": True, "value": 1}]
    )
    assert isinstance(outcome, dict) and outcome.get("success"), f"got {outcome!r}"
    assert attempts == 3, f"expected 3 attempts, got {attempts}"


def test_5xx_exhausts_attempts_and_raises() -> None:
    outcome, attempts = _run_dispatch([_status_error(503)])
    assert isinstance(outcome, httpx.HTTPStatusError)
    assert attempts == 3


def test_4xx_fails_fast_without_retry() -> None:
    for code in (404, 429):
        outcome, attempts = _run_dispatch([_status_error(code)])
        assert isinstance(outcome, httpx.HTTPStatusError), f"{code}: got {outcome!r}"
        assert attempts == 1, f"{code}: expected 1 attempt, got {attempts}"


def main() -> int:
    failures = []
    for fn in (
        test_5xx_retries_then_succeeds,
        test_5xx_exhausts_attempts_and_raises,
        test_4xx_fails_fast_without_retry,
    ):
        print(f"--- {fn.__name__} ---")
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            failures.append(f"{fn.__name__}: {exc!r}")
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
