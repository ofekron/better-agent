"""Unit test for the run_delegation capability guard: a fork-mode
delegation against a provider with supports_fork=False is rejected with
a clean error (delegate_error_payload) BEFORE minting a fork/spawning,
instead of raising NotImplementedError -> HTTP 500.

(The classic supports_fork=False case today is gemini, via
gemini-cli#22563. Codex supports fork via the app-server `thread/fork`,
so it passes this guard and is covered by
integration_test_codex_fork.py.)

Run:  cd backend && .venv/bin/python scripts/test_delegation_rejects_nonfork_provider.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# Per CLAUDE.md, isolate ~/.better-claude state to a tempdir BEFORE
# importing any backend module so the dev's real session store is untouched.
import _test_home
_test_home.isolate("bc-test-del-nonfork-")
for _sub in ("sessions", "runs", "ask-status", "delegate-status"):
    (Path(os.environ["BETTER_AGENT_HOME"]) / _sub).mkdir(parents=True, exist_ok=True)

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchs.manager import _delegation  # noqa: E402

CALLER_SID = "manager-session"
WORKER_SID = "worker-session"
WORKER_CWD = str(Path("/tmp/worker-project").resolve())


class _Provider:
    def __init__(self, supports_fork: bool, kind: str):
        self.supports_fork = supports_fork
        self.KIND = kind


class _TurnManager:
    cancel_events: dict = {}
    current_turn_workers: dict = {}
    current_assistant_msgs: dict = {}

    def get_turn_save_callback(self, app_session_id: str):
        return None

    def in_flight_event_count(self, app_session_id: str):
        return 0

    def in_flight_event_count_after_current_event(self, app_session_id: str):
        return 0

    def run_state_add(self, *args, **kwargs):
        pass

    def run_state_remove(self, *args, **kwargs):
        pass

    async def emit_run_state(self, app_session_id: str):
        pass


class _FakeCoordinator:
    pair_locks: dict = {}
    active_delegations: dict = {}
    turn_manager = _TurnManager()

    def __init__(self, provider: _Provider):
        self._provider = provider

    def provider_for_session(self, agent_session_id: str):
        return self._provider

    def known_worker_registry_cwd(self, app_session_id: str, agent_session_id: str):
        return WORKER_CWD

    async def persist_and_dispatch_raw(self, app_session_id: str, event: dict):
        pass

    async def _init_target_agent_session(self, *args, **kwargs):
        raise AssertionError("non-fork provider must be rejected before target init")


def test_ephemeral_fork_delegations_do_not_share_pair_lock():
    coord = _FakeCoordinator(_Provider(supports_fork=True, kind="claude"))
    persistent_a = _delegation.lock_for_delegation(
        coord, CALLER_SID, WORKER_SID, "fork", ephemeral=False,
    )
    persistent_b = _delegation.lock_for_delegation(
        coord, CALLER_SID, WORKER_SID, "fork", ephemeral=False,
    )
    ephemeral_a = _delegation.lock_for_delegation(
        coord, CALLER_SID, WORKER_SID, "fork", ephemeral=True,
    )
    ephemeral_b = _delegation.lock_for_delegation(
        coord, CALLER_SID, WORKER_SID, "fork", ephemeral=True,
    )

    assert persistent_a is persistent_b
    assert ephemeral_a is not ephemeral_b
    assert ephemeral_a is not persistent_a


def _fake_get_worker(cwd: str, agent_session_id: str):
    if agent_session_id != WORKER_SID:
        return None
    return {
        "agent_session_id": WORKER_SID,
        "orchestration_mode": "native",
        "agent_sid": "agent-parent",
        "node_id": "primary",
    }


def _session_record(*, agent_session_id: str | None = "agent-parent"):
    return {
        "id": WORKER_SID,
        "name": "worker",
        "orchestration_mode": "native",
        "agent_session_id": agent_session_id,
        "model": "claude-sonnet-4-6",
        "cwd": WORKER_CWD,
    }


def _fake_session_get(agent_session_id: str):
    if agent_session_id != WORKER_SID:
        return None
    return _session_record()


def _fake_uninitialized_session_get(agent_session_id: str):
    if agent_session_id != WORKER_SID:
        return None
    return _session_record(agent_session_id=None)


def _patch(monkeypatch, locked_called: list):
    monkeypatch.setattr(_delegation.worker_store, "get_worker", _fake_get_worker)
    monkeypatch.setattr(_delegation.session_manager, "get", _fake_session_get)

    async def _fake_locked(*args, **kwargs):
        # In production this is where start_run(fork=True) would raise
        # NotImplementedError for a non-fork provider. The guard must
        # short-circuit before we ever get here for such providers.
        locked_called.append(True)
        return {"success": True, "worker_session_id": WORKER_SID}

    monkeypatch.setattr(_delegation, "run_delegation_locked", _fake_locked)


def test_fork_rejected_for_nonfork_provider(monkeypatch):
    locked_called: list = []
    _patch(monkeypatch, locked_called)
    coord = _FakeCoordinator(_Provider(supports_fork=False, kind="gemini"))

    result = asyncio.run(_delegation.run_delegation(
        coord,
        app_session_id=CALLER_SID,
        instructions="do isolated review",
        worker_session_id=WORKER_SID,
        worker_description="worker",
        model="gemini-2.5-pro",
        cwd="/tmp",
        run_mode="fork",
        ephemeral=True,
    ))

    assert result["success"] is False
    assert "does not support fork" in result["error"]
    assert result["worker_session_id"] == WORKER_SID
    assert locked_called == [], "run_delegation_locked must not run for a non-fork provider"


def test_nonfork_provider_rejected_before_missing_parent_init(monkeypatch):
    locked_called: list = []
    _patch(monkeypatch, locked_called)
    monkeypatch.setattr(_delegation.session_manager, "get", _fake_uninitialized_session_get)
    coord = _FakeCoordinator(_Provider(supports_fork=False, kind="gemini"))

    result = asyncio.run(_delegation.run_delegation(
        coord,
        app_session_id=CALLER_SID,
        instructions="do isolated review",
        worker_session_id=WORKER_SID,
        worker_description="worker",
        model="gemini-2.5-pro",
        cwd="/tmp",
        run_mode="fork",
        ephemeral=True,
    ))

    assert result["success"] is False
    assert "does not support fork" in result["error"]
    assert locked_called == []


def test_fork_allowed_for_fork_capable_provider(monkeypatch):
    locked_called: list = []
    _patch(monkeypatch, locked_called)
    coord = _FakeCoordinator(_Provider(supports_fork=True, kind="claude"))

    result = asyncio.run(_delegation.run_delegation(
        coord,
        app_session_id=CALLER_SID,
        instructions="do isolated review",
        worker_session_id=WORKER_SID,
        worker_description="worker",
        model="claude-sonnet-4-6",
        cwd="/tmp",
        run_mode="fork",
        ephemeral=True,
    ))

    assert locked_called == [True]
    assert result["success"] is True


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
