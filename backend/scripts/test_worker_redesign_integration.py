from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home

_HOME = _test_home.TestHome.acquire_installed("ba-worker-redesign-integration-")

import auth  # noqa: E402
import config_store  # noqa: E402
import main  # noqa: E402
import pytest  # noqa: E402
import session_queue_projection  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from orchs.manager import _approval as approval  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from stores import pending_approvals, worker_store  # noqa: E402

pytestmark = pytest.mark.anyio


def _engage_home() -> None:
    _test_home.engage(_HOME.path)
    assert session_manager.get("__worker_redesign_home_alignment__") is None


def setup_function(_function) -> None:
    _engage_home()


@pytest.fixture(scope="module")
def app_client():
    _engage_home()
    patcher = pytest.MonkeyPatch()
    patcher.setattr("app_lifecycle.acquire_backend_instance_lock", lambda: None)
    patcher.setattr("app_lifecycle.release_backend_instance_lock", lambda: None)
    try:
        with TestClient(main.app) as test_client:
            token = auth.create_token("worker-redesign-integration")
            test_client.headers.update({"Authorization": f"Bearer {token}"})
            yield test_client, token
    finally:
        patcher.undo()


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_subscription_acknowledges_before_dependent_work(app_client) -> None:
    test_client, token = app_client
    session = session_manager.create(
        name="subscription-ready",
        model="test-model",
        cwd=_HOME.path,
        orchestration_mode="native",
    )
    try:
        with test_client.websocket_connect(f"/ws/chat?token={token}") as websocket:
            websocket.send_json({
                "type": "subscribe",
                "app_session_id": session["id"],
                "cwd": _HOME.path,
            })
            while True:
                replay = websocket.receive_json()
                if replay.get("type") == "messages_replay":
                    break
        assert replay["type"] == "messages_replay"
        assert replay["data"]["app_session_id"] == session["id"]
    finally:
        session_manager.delete(session["id"])


async def test_approval_waiter_drives_requested_and_approved_events(monkeypatch) -> None:
    delegation_id = "deterministic-worker-approval"
    events: list[dict] = []
    provider_init_calls: list[dict] = []
    requested = asyncio.Event()
    provider_id = config_store.default_session_provider_id()
    provider = config_store.get_provider(provider_id or "")
    assert provider_id and provider is not None
    coordinator = SimpleNamespace(
        approval_waiters={},
        init_cancel_events={},
        provider_for_session=lambda _sid: SimpleNamespace(
            id=provider_id,
            record=provider,
        ),
    )

    async def broadcast_workers_changed(_cwd) -> None:
        return None

    async def fake_provider_init(_self, _coordinator, **_kwargs) -> str:
        provider_init_calls.append(_kwargs)
        return "fake-provider-session"

    async def ws_callback(event: dict) -> None:
        events.append(event)
        if event.get("type") == "worker_creation_requested":
            requested.set()

    coordinator.broadcast_workers_changed = broadcast_workers_changed
    monkeypatch.setattr(approval.SubprocessAgent, "init", fake_provider_init)
    task = asyncio.create_task(approval.await_fresh_worker_approval(
        coordinator,
        delegation_id=delegation_id,
        app_session_id="manager-session",
        cwd=_HOME.path,
        justification="deterministic approval proof",
        proposed_description="approved-worker",
        proposed_orchestration_mode="native",
        instructions_preview="work",
        model="test-model",
        ws_callback=ws_callback,
        cancel_event=asyncio.Event(),
    ))
    worker_id = ""
    try:
        await requested.wait()
        assert delegation_id in coordinator.approval_waiters
        record, reason = pending_approvals.approve(delegation_id)
        assert reason == "ok"
        assert approval.resolve_approval(coordinator, delegation_id, record)
        worker = await asyncio.wait_for(task, timeout=2)
        assert worker is not None
        worker_id = worker["agent_session_id"]
        assert [event["type"] for event in events] == [
            "worker_creation_requested",
            "worker_creation_approved",
        ]
        assert len(provider_init_calls) == 1
        assert worker_store.get_worker(_HOME.path, worker_id) is not None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        pending_approvals.delete(delegation_id)
        if worker_id:
            worker_store.remove_worker(_HOME.path, worker_id)
            session_manager.delete(worker_id)


def test_session_delete_fans_out_worker_and_fork_cleanup(app_client) -> None:
    test_client, _ = app_client
    caller = session_manager.create(
        name="fanout-caller",
        model="test-model",
        cwd=_HOME.path,
        orchestration_mode="native",
    )
    worker = session_manager.create(
        name="fanout-worker",
        model="test-model",
        cwd=_HOME.path,
        orchestration_mode="native",
    )
    fork = session_manager.create(
        name="fanout-fork",
        model="test-model",
        cwd=_HOME.path,
        orchestration_mode="native",
    )
    worker_store.upsert_worker(
        cwd=_HOME.path,
        agent_session_id=worker["id"],
        orchestration_mode="native",
        agent_sid="fake-provider-worker",
    )
    worker_store.set_fork(_HOME.path, caller["id"], worker["id"], fork["id"])
    try:
        response = test_client.delete(f"/api/sessions/{worker['id']}")
        assert response.status_code == 200, response.text
        assert worker_store.get_worker(_HOME.path, worker["id"]) is None
        assert worker_store.get_fork_record(
            _HOME.path, caller["id"], worker["id"]
        ) is None
        assert session_manager.get(fork["id"]) is None
    finally:
        for session_id in (fork["id"], worker["id"], caller["id"]):
            session_manager.delete(session_id)


async def test_live_smoke_scripts_skip_before_starting_backend(monkeypatch) -> None:
    import integration_test
    import integration_test_advanced

    monkeypatch.delenv("RUN_LLM_TESTS", raising=False)

    def unexpected_start(_self) -> None:
        raise AssertionError("gated live smoke started its backend")

    monkeypatch.setattr(integration_test.BackgroundUvicorn, "start", unexpected_start)
    monkeypatch.setattr(
        integration_test_advanced.BackgroundUvicorn,
        "start",
        unexpected_start,
    )
    assert await integration_test.main() == 0
    assert await integration_test_advanced.main() == 0


def teardown_module() -> None:
    session_manager.flush_pending_persists()
    session_queue_projection.shutdown(timeout=None)
    _HOME.release()
