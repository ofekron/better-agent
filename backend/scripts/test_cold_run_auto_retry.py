"""Cold-recovered failed runs must get the bounded auto-retry.

Locks the cold-path retry gate added to `_integrate_one_locked`: a run
that completed with a retryable pre-provider failure while the backend
was down (runner bootstrap broker vanished mid-restart) is re-issued via
`_retry_recovered_run` instead of dying with only an error stamp — the
exact loss reported for session 8854b866 / run 98b47fbe.

Negative gates locked too: newer-turn sessions, cancelled runs, and an
exhausted transient budget must NOT auto-retry (error stamp only).

Run: cd backend && .venv/bin/python scripts/test_cold_run_auto_retry.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-cold-run-auto-retry-")

import _test_installation  # noqa: E402
_test_installation.activate(Path(_TMP_HOME))

from session_manager import manager as session_manager  # noqa: E402
from ingestion_versions import CLAUDE_INGESTION_VERSION  # noqa: E402
from provider import default_provider  # noqa: E402
from provider_claude import _runs_root  # noqa: E402
import run_recovery  # noqa: E402
from run_recovery import integrate_recovered_runs  # noqa: E402

pytestmark = pytest.mark.anyio

_BOOTSTRAP_ERROR = (
    "runtime bootstrap unavailable before the turn started (backend was "
    "restarting or the one-shot lease expired): [Errno 61] Connection refused"
)


def _seed_session(*, transient_attempt: int = 0) -> tuple[str, str]:
    sess = session_manager.create(
        name="t", model="glm-5.1", cwd="/tmp", orchestration_mode="native",
    )
    sid = sess["id"]
    user_msg = {
        "id": str(uuid.uuid4()),
        "role": "user",
        "content": "do a thing",
        "events": [],
        "isStreaming": False,
    }
    from orchs import get_strategy
    asst_msg = get_strategy("native").build_assistant_scaffold()
    asst_msg["isStreaming"] = True
    if transient_attempt:
        asst_msg["transient_attempt"] = transient_attempt
    session_manager.append_user_msg(sid, user_msg)
    session_manager.append_assistant_msg(sid, asst_msg)
    return sid, asst_msg["id"]


def _seed_never_started_run(
    app_sid: str, target_message_id: str, *, cancelled: bool = False,
) -> str:
    """Run dir shaped exactly like the incident: complete.json written by
    the runner's pre-provider _fail, no state.json, dead pid."""
    run_id = str(uuid.uuid4())
    run_dir = _runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "input.json").write_text(json.dumps({
        "prompt": "do a thing", "cwd": "/tmp", "model": "glm-5.1",
        "session_id": None, "mode": "native", "app_session_id": app_sid,
        "fork": False, "target_message_id": target_message_id,
    }))
    (run_dir / "backend_state.json").write_text(json.dumps({
        "run_id": run_id, "app_session_id": app_sid, "mode": "native",
        "runner_pid": 0, "session_id": None, "jsonl_path": None,
        "processed_byte": 0, "cancelled": cancelled,
        "ingestion_version": CLAUDE_INGESTION_VERSION,
        "target_message_id": target_message_id,
    }))
    (run_dir / "pid").write_text("0")
    (run_dir / "complete.json").write_text(json.dumps({
        "success": False,
        "session_id": None,
        "error": "cancelled" if cancelled else _BOOTSTRAP_ERROR,
        "error_kind": None if cancelled else "runtime_bootstrap_unavailable",
        "pre_provider": True,
        "token_usage": None,
        "finished_at": "2026-07-30T20:43:17.831473",
    }))
    return run_id


class _RetryRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, **kwargs) -> None:
        self.calls.append(kwargs)


async def _recover_with_recorder() -> _RetryRecorder:
    recorder = _RetryRecorder()
    original = run_recovery._retry_recovered_run
    run_recovery._retry_recovered_run = recorder
    try:
        bridge = default_provider()
        recovered = bridge.recover_in_flight()
        await integrate_recovered_runs(coordinator=None, recovered=recovered)
    finally:
        run_recovery._retry_recovered_run = original
    return recorder


def _msg(sid: str, msg_id: str) -> dict:
    sess = session_manager.get(sid) or {}
    return next(
        (m for m in sess.get("messages", []) if m.get("id") == msg_id), {},
    )


async def test_never_started_run_auto_retries() -> None:
    sid, asst_id = _seed_session()
    run_id = _seed_never_started_run(sid, asst_id)
    recorder = await _recover_with_recorder()
    assert len(recorder.calls) == 1, (
        f"expected 1 retry call, got {len(recorder.calls)}"
    )
    call = recorder.calls[0]
    assert call.get("msg_id") == asst_id, (
        f"retry targeted {call.get('msg_id')!r}, want {asst_id!r}"
    )
    marker = _runs_root() / run_id / "reconciled.marker"
    assert marker.exists(), "old run not reconciled after retry handoff"


async def test_newer_turn_blocks_auto_retry() -> None:
    sid, asst_id = _seed_session()
    run_id = _seed_never_started_run(sid, asst_id)
    # User moved on: a newer user+assistant pair exists.
    session_manager.append_user_msg(sid, {
        "id": str(uuid.uuid4()), "role": "user", "content": "newer",
        "events": [], "isStreaming": False,
    })
    from orchs import get_strategy
    newer_asst = get_strategy("native").build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, newer_asst)
    recorder = await _recover_with_recorder()
    assert not recorder.calls, (
        f"retry fired for a non-latest target: {len(recorder.calls)}"
    )
    assert (_runs_root() / run_id / "reconciled.marker").exists(), (
        "non-latest cold run should still be reconciled"
    )


async def test_cancelled_run_blocks_auto_retry() -> None:
    sid, asst_id = _seed_session()
    _seed_never_started_run(sid, asst_id, cancelled=True)
    recorder = await _recover_with_recorder()
    assert not recorder.calls, (
        f"retry fired for a cancelled run: {len(recorder.calls)}"
    )


async def test_exhausted_budget_stamps_error_instead() -> None:
    sid, asst_id = _seed_session(transient_attempt=10)
    _seed_never_started_run(sid, asst_id)
    recorder = await _recover_with_recorder()
    assert not recorder.calls, "retry fired past the transient budget"
    msg = _msg(sid, asst_id)
    assert msg.get("error"), (
        f"budget-exhausted run left no error stamp: {msg.keys()}"
    )
    assert "bootstrap" in str(msg.get("errorText") or ""), (
        f"errorText not surfaced: {msg.get('errorText')!r}"
    )


async def main() -> int:
    for fn in (
        test_never_started_run_auto_retries,
        test_newer_turn_blocks_auto_retry,
        test_cancelled_run_blocks_auto_retry,
        test_exhausted_budget_stamps_error_instead,
    ):
        try:
            await fn()
        except AssertionError as exc:
            print(f"FAIL {fn.__name__}: {exc}")
            return 1
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
            return 1
        print(f"PASS {fn.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
