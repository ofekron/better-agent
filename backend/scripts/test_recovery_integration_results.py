from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _test_home  # noqa: E402

_test_home.isolate("recovery-integration-results-")

import containment as containment_module  # noqa: E402
import run_recovery  # noqa: E402
from ingestion_versions import current_ingestion_version  # noqa: E402
from runs_dir import runs_root  # noqa: E402

pytestmark = pytest.mark.anyio


class _SessionManager:
    def __init__(self, session: dict | None) -> None:
        self.session = session

    def get(self, _session_id: str) -> dict | None:
        return self.session

    def set_msg_recovering(self, *_args) -> None:
        return None


class _Provider:
    KIND = "claude"
    _runs: dict = {}

    def cancel_run(self, _run_id: str) -> None:
        return None


def _descriptor(run_id: str, **updates) -> dict:
    (runs_root() / run_id).mkdir(parents=True, exist_ok=True)
    desc = {
        "run_id": run_id,
        "app_session_id": "sid",
        "persist_to": "sid",
        "alive": False,
        "has_complete_json": False,
        "cancelled": False,
        "provider_kind": "claude",
        "ingestion_version": current_ingestion_version("claude"),
    }
    desc.update(updates)
    return desc


async def _integrate(desc: dict, session: dict | None):
    return await run_recovery._integrate_one_locked(
        None,
        _Provider(),
        desc,
        summary=None,
        recovery_root_id=None,
        root_lease=None,
    )


async def test_live_missing_session_is_retryable(monkeypatch) -> None:
    monkeypatch.setattr(run_recovery, "session_manager", _SessionManager(None))
    result = await _integrate(
        _descriptor("missing-live-session", alive=True),
        None,
    )
    assert result == "retryable"


async def test_completed_missing_session_is_success(monkeypatch) -> None:
    monkeypatch.setattr(run_recovery, "session_manager", _SessionManager(None))
    result = await _integrate(
        _descriptor("missing-completed-session", has_complete_json=True),
        None,
    )
    assert result == "success"
    assert (
        runs_root()
        / "missing-completed-session"
        / "reconciled.marker"
    ).exists()


async def test_surviving_legacy_pid_is_retryable(monkeypatch) -> None:
    class _Containment:
        def reattach(self, *_args) -> None:
            return None

        def force_kill_all(self, *_args) -> None:
            return None

        def teardown(self, *_args) -> None:
            return None

    session = {"kind": "adv_sync_fork", "messages": []}
    monkeypatch.setattr(run_recovery, "session_manager", _SessionManager(session))
    monkeypatch.setattr(run_recovery, "live_recovery_pid", lambda _desc: 4242)
    monkeypatch.setattr(run_recovery, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(containment_module, "containment", lambda: _Containment())
    result = await _integrate(_descriptor("surviving-legacy-pid"), session)
    assert result == "retryable"


async def test_live_old_pipeline_without_source_is_retryable(monkeypatch) -> None:
    session = {"kind": "session", "messages": []}
    monkeypatch.setattr(run_recovery, "session_manager", _SessionManager(session))
    result = await _integrate(
        _descriptor(
            "live-old-pipeline",
            alive=True,
            ingestion_version=0,
            jsonl_path=str(runs_root() / "missing.jsonl"),
        ),
        session,
    )
    assert result == "retryable"


async def test_capability_change_without_target_is_retryable(monkeypatch) -> None:
    session = {
        "kind": "session",
        "messages": [{"id": "target", "role": "assistant", "events": []}],
    }
    monkeypatch.setattr(run_recovery, "session_manager", _SessionManager(session))
    monkeypatch.setattr(
        run_recovery,
        "_is_provider_capability_change",
        lambda _run_id: True,
    )
    monkeypatch.setattr(
        run_recovery,
        "_recovery_target_assistant",
        lambda *_args: None,
    )
    result = await _integrate(
        _descriptor(
            "capability-missing-target",
            has_complete_json=True,
            target_message_id="target",
            replay_end_byte=0,
        ),
        session,
    )
    assert result == "retryable"


async def test_replay_persist_failure_is_retryable(monkeypatch) -> None:
    session = {
        "kind": "session",
        "messages": [{"id": "target", "role": "assistant", "events": []}],
    }
    session_manager = _SessionManager(session)

    async def attach_selector(*_args, **_kwargs):
        return None

    def fail_integration(**_kwargs) -> None:
        raise RuntimeError("persist failed")

    monkeypatch.setattr(run_recovery, "session_manager", session_manager)
    monkeypatch.setattr(run_recovery, "_is_consistent", lambda *_args: False)
    monkeypatch.setattr(
        run_recovery,
        "_attach_recovered_selector_sid",
        attach_selector,
    )
    monkeypatch.setattr(run_recovery, "_apply_integration_sync", fail_integration)
    result = await _integrate(
        _descriptor(
            "persist-failure",
            target_message_id="target",
        ),
        session,
    )
    assert result == "retryable"
    assert not (runs_root() / "persist-failure" / "reconciled.marker").exists()
