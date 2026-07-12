from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _test_home

_test_home.isolate("bc-test-session-bridge-organization-")

import session_bridge  # noqa: E402
import session_organization_store  # noqa: E402
from event_bus import bus  # noqa: E402


async def test_new_session_assignment_precedes_turn() -> None:
    folder = session_organization_store.create_folder(project_id="/repo", name="Folder")
    tag = session_organization_store.create_tag(project_id="/repo", name="Tag")
    original_get = session_bridge.session_manager.get
    original_create = session_bridge.session_manager.create
    original_run_turn = session_bridge._run_turn
    events: list[dict] = []

    session_bridge.session_manager.get = lambda _sid: {  # type: ignore[assignment]
        "id": "caller", "name": "Caller", "cwd": "/repo",
        "orchestration_mode": "native", "model": "model",
    }
    session_bridge.session_manager.create = lambda **_kwargs: {  # type: ignore[assignment]
        "id": "new-session",
    }

    async def fake_turn(sid: str, _prompt: str, **_kwargs) -> dict:
        organization = session_organization_store.organization_for_session(sid)
        assert organization["folder_id"] == folder["id"]
        assert organization["tag_ids"] == [tag["id"]]
        return {"text": "done", "turn_id": "turn"}

    async def record(event) -> None:
        events.append(event.payload)

    session_bridge._run_turn = fake_turn  # type: ignore[assignment]
    bus.subscribe(
        "session.organization_changed", record,
        name="test_session_bridge_organization",
    )
    try:
        result = await session_bridge._run_new(
            "caller", "prompt", folder_id=folder["id"], tag_ids=[tag["id"]],
        )
    finally:
        bus.unsubscribe("test_session_bridge_organization")
        session_bridge.session_manager.get = original_get  # type: ignore[assignment]
        session_bridge.session_manager.create = original_create  # type: ignore[assignment]
        session_bridge._run_turn = original_run_turn  # type: ignore[assignment]
    assert result["session_id"] == "new-session"
    assert events == [{"session_ids": ["new-session"]}]


async def test_existing_target_ignores_organization_options() -> None:
    original_index = session_bridge.session_search.index_stub_map
    original_in_flight = session_bridge._caller_in_flight_msg_id
    original_run = session_bridge._run
    original_pref = session_bridge.user_prefs.get_cross_session_delegate_auto
    session_bridge.session_search.index_stub_map = lambda: {"target": {"id": "target"}}
    session_bridge._caller_in_flight_msg_id = lambda _sid: "message"  # type: ignore[assignment]
    session_bridge.user_prefs.get_cross_session_delegate_auto = lambda: True  # type: ignore[assignment]

    async def fake_run(target_sid: str, _prompt: str, _mode: str, **_kwargs) -> dict:
        return {"session_id": target_sid}

    session_bridge._run = fake_run  # type: ignore[assignment]
    try:
        result = await session_bridge.delegate(
            caller_sid="caller", target_sid="target", prompt="prompt",
            run_mode="fork", approval="auto",
            folder_id="missing-folder", tag_ids=["missing-tag"],
        )
    finally:
        session_bridge.session_search.index_stub_map = original_index
        session_bridge._caller_in_flight_msg_id = original_in_flight  # type: ignore[assignment]
        session_bridge._run = original_run  # type: ignore[assignment]
        session_bridge.user_prefs.get_cross_session_delegate_auto = original_pref  # type: ignore[assignment]
    assert result["session_id"] == "target"


def test_mcp_payload_forwards_organization() -> None:
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "sdk"))
    path = root / "extensions" / "session-bridge" / "mcp" / "server.py"
    spec = importlib.util.spec_from_file_location("session_bridge_mcp_org_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured: dict = {}

    class FakeClient:
        def target_session(self, _explicit="") -> str:
            return "caller"

        def invoke_durable(self, _action, _operation, payload, *, timeout):
            captured.update(payload)
            return {"success": True}

    module.SessionBridgeClient = FakeClient
    module.delegate_to_session_response(
        "prompt", "fork", "require",
        folder_id="folder", tag_ids=["tag"],
    )
    assert captured["folder_id"] == "folder"
    assert captured["tag_ids"] == ["tag"]


def main() -> int:
    asyncio.run(test_new_session_assignment_precedes_turn())
    asyncio.run(test_existing_target_ignores_organization_options())
    test_mcp_payload_forwards_organization()
    print("PASS session bridge organization options")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
