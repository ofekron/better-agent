from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate_installed("bc-test-ws-selectors-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

from auth_test_helpers import authenticate_client  # noqa: E402
import auth  # noqa: E402
import main  # noqa: E402
import session_detail_api  # noqa: E402
import config_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_ws_send_uses_backend_owned_session_selectors() -> None:
    captured: list[dict] = []
    original_submit = main.coordinator.submit_prompt_async
    provider = config_store.get_default_provider() or {}
    session = session_manager.create(
        name="selector-authority",
        cwd="/tmp/backend-owned",
        model="backend-model",
        provider_id=provider.get("id"),
        orchestration_mode="native",
    )

    async def fake_submit_prompt_async(app_session_id: str, params: dict) -> str:
        captured.append({"app_session_id": app_session_id, **params})
        return params["_queued_id"]

    main.coordinator.submit_prompt_async = fake_submit_prompt_async
    try:
        client = TestClient(main.app, client=("127.0.0.1", 50000))
        authenticate_client(client)
        token = auth.create_token("test")
        with client.websocket_connect(f"/ws/chat?token={token}") as ws:
            ws.send_json({
                "type": "send_message",
                "app_session_id": session["id"],
                "prompt": "use authoritative selectors",
                "model": "frontend-stale-model",
                "cwd": "/tmp/frontend-stale",
                "orchestration_mode": "team",
                "client_id": "selector-authority-client",
            })
            for _ in range(40):
                if captured:
                    break
                time.sleep(0.05)
    finally:
        main.coordinator.submit_prompt_async = original_submit

    assert captured, "submit_prompt_async was not called"
    params = captured[0]
    assert params.get("model") == "backend-model", f"model came from frontend: {params.get('model')!r}"
    assert params.get("cwd") == "/tmp/backend-owned", f"cwd came from frontend: {params.get('cwd')!r}"
    assert params.get("orchestration_mode") == "native", (
        f"orchestration_mode came from frontend: {params.get('orchestration_mode')!r}"
    )


def test_ws_send_forwards_disallowed_tools() -> None:
    captured: list[dict] = []
    original_submit = main.coordinator.submit_prompt_async
    provider = config_store.get_default_provider() or {}
    session = session_manager.create(
        name="disallowed-tools",
        cwd="/tmp/disallowed-tools",
        model="backend-model",
        provider_id=provider.get("id"),
        orchestration_mode="native",
    )

    async def fake_submit_prompt_async(app_session_id: str, params: dict) -> str:
        captured.append({"app_session_id": app_session_id, **params})
        return params["_queued_id"]

    main.coordinator.submit_prompt_async = fake_submit_prompt_async
    try:
        client = TestClient(main.app, client=("127.0.0.1", 50001))
        authenticate_client(client)
        token = auth.create_token("test")
        with client.websocket_connect(f"/ws/chat?token={token}") as ws:
            ws.send_json({
                "type": "send_message",
                "app_session_id": session["id"],
                "prompt": "restricted turn",
                "model": "backend-model",
                "cwd": "/tmp/disallowed-tools",
                "orchestration_mode": "native",
                "client_id": "disallowed-tools-client",
                "disallowed_tools": [" Bash ", "Edit"],
            })
            for _ in range(40):
                if captured:
                    break
                time.sleep(0.05)
    finally:
        main.coordinator.submit_prompt_async = original_submit

    assert captured, "submit_prompt_async was not called"
    assert captured[0].get("disallowed_tools") == ["Bash", "Edit"], (
        f"disallowed_tools not forwarded: {captured[0].get('disallowed_tools')!r}"
    )


def test_ws_disallowed_tools_validation() -> None:
    assert session_detail_api._parse_ws_disallowed_tools(None) is None, "None should stay None"
    assert session_detail_api._parse_ws_disallowed_tools([" Bash ", "Edit"]) == ["Bash", "Edit"], (
        "valid entries should be trimmed"
    )
    try:
        session_detail_api._parse_ws_disallowed_tools("Bash")
    except ValueError as e:
        assert str(e) == "disallowed_tools must be an array", f"unexpected scalar error: {e}"
    else:
        raise AssertionError("scalar input should be rejected")
    try:
        session_detail_api._parse_ws_disallowed_tools([""])
    except ValueError as e:
        assert str(e) == "disallowed_tools entries must be non-empty strings", f"unexpected entry error: {e}"
    else:
        raise AssertionError("empty entries should be rejected")


def main_run() -> int:
    tests = [
        ("ws send uses backend-owned session selectors", test_ws_send_uses_backend_owned_session_selectors),
        ("ws send forwards disallowed tools", test_ws_send_forwards_disallowed_tools),
        ("ws disallowed tools validation", test_ws_disallowed_tools_validation),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"  exception: {e}")
            print(f"{FAIL}  {name}")
            continue
        print(f"{PASS}  {name}")
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main_run())
