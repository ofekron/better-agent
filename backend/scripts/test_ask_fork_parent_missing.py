from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("ba-test-ask-fork-parent-missing-")

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi import HTTPException  # noqa: E402
import config_store  # noqa: E402
import main  # noqa: E402
from session_manager import (  # noqa: E402
    DelegateForkParentMissing,
    manager as session_manager,
)


def test_create_delegate_fork_missing_parent_raises_typed_keyerror():
    """create_delegate_fork raises DelegateForkParentMissing (a KeyError
    subclass — so existing `except KeyError` catchers still catch it) when the
    parent agent session is gone."""
    try:
        session_manager.create_delegate_fork(
            parent_agent_session_id="definitely-missing-agent-session-id",
            caller_agent_session_id="caller",
            parent_agent_sid_at_fork="caller",
            parent_line_count_at_fork=0,
            orchestration_mode="native",
        )
        raise AssertionError("missing parent did not raise")
    except DelegateForkParentMissing:
        pass
    # Must also still be catchable as KeyError (preserves the strict contract).
    try:
        session_manager.create_delegate_fork(
            parent_agent_session_id="definitely-missing-agent-session-id",
            caller_agent_session_id="caller",
            parent_agent_sid_at_fork="caller",
            parent_line_count_at_fork=0,
            orchestration_mode="native",
        )
        raise AssertionError("missing parent did not raise on second call")
    except KeyError:
        pass


def test_ask_fork_missing_parent_returns_409_not_500():
    """When the parent agent session vanishes mid-delegation (race), the
    DelegateForkParentMissing raised from run_delegation must surface as a
    409 HTTPException at the ask-fork boundary — NOT a bare 500.

    Regression: previously the strict-mode KeyError propagated unhandled
    through ASGI as a 500 + ExceptionGroup (~4 occurrences in logs)."""
    async def raise_parent_missing(**_kwargs):
        raise DelegateForkParentMissing("parent-agent-session-id")

    target = session_manager.create(
        name="target",
        cwd="/tmp",
        orchestration_mode="native",
        model="model",
        source="test",
    )
    provider_id = config_store.list_providers()["providers"][0]["id"]
    body = {
        "app_session_id": "caller-session",
        "instructions": "check this",
        "worker_session_id": target["id"],
        "worker_description": "",
        "provider_id": provider_id,
        "model": "model",
        "reasoning_effort": "high",
        "cwd": "/tmp",
        "run_mode": "fork",
    }
    original = main.coordinator.run_delegation
    main.coordinator.run_delegation = raise_parent_missing
    error = None
    try:
        try:
            asyncio.run(main.internal_ask_fork(
                body,
                x_internal_token=main.coordinator.internal_token,
            ))
        except HTTPException as exc:
            error = exc
        else:
            raise AssertionError("missing-parent delegation did not raise HTTPException")
    finally:
        main.coordinator.run_delegation = original

    assert error is not None
    assert error.status_code == 409, error.detail
    assert "no longer available" in error.detail


if __name__ == "__main__":
    test_create_delegate_fork_missing_parent_raises_typed_keyerror()
    test_ask_fork_missing_parent_returns_409_not_500()
    print("ok")
