from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-ws-selectors-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

from auth_test_helpers import authenticate_client  # noqa: E402
import auth  # noqa: E402
import main  # noqa: E402
import config_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_ws_send_uses_backend_owned_session_selectors() -> bool:
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
        with TestClient(main.app, client=("127.0.0.1", 50000)) as client:
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

    if not captured:
        print("  submit_prompt_async was not called")
        return False
    params = captured[0]
    if params.get("model") != "backend-model":
        print(f"  model came from frontend: {params.get('model')!r}")
        return False
    if params.get("cwd") != "/tmp/backend-owned":
        print(f"  cwd came from frontend: {params.get('cwd')!r}")
        return False
    if params.get("orchestration_mode") != "native":
        print(f"  orchestration_mode came from frontend: {params.get('orchestration_mode')!r}")
        return False
    return True


def test_ws_send_forwards_disallowed_tools() -> bool:
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
        with TestClient(main.app, client=("127.0.0.1", 50001)) as client:
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

    if not captured:
        print("  submit_prompt_async was not called")
        return False
    if captured[0].get("disallowed_tools") != ["Bash", "Edit"]:
        print(f"  disallowed_tools not forwarded: {captured[0].get('disallowed_tools')!r}")
        return False
    return True


def test_ws_disallowed_tools_validation() -> bool:
    if main._parse_ws_disallowed_tools(None) is not None:
        print("  None should stay None")
        return False
    if main._parse_ws_disallowed_tools([" Bash ", "Edit"]) != ["Bash", "Edit"]:
        print("  valid entries should be trimmed")
        return False
    try:
        main._parse_ws_disallowed_tools("Bash")
    except ValueError as e:
        if str(e) != "disallowed_tools must be an array":
            print(f"  unexpected scalar error: {e}")
            return False
    else:
        print("  scalar input should be rejected")
        return False
    try:
        main._parse_ws_disallowed_tools([""])
    except ValueError as e:
        if str(e) != "disallowed_tools entries must be non-empty strings":
            print(f"  unexpected entry error: {e}")
            return False
    else:
        print("  empty entries should be rejected")
        return False
    return True


def main_run() -> int:
    tests = [
        ("ws send uses backend-owned session selectors", test_ws_send_uses_backend_owned_session_selectors),
        ("ws send forwards disallowed tools", test_ws_send_forwards_disallowed_tools),
        ("ws disallowed tools validation", test_ws_disallowed_tools_validation),
    ]
    failed = 0
    for name, fn in tests:
        try:
            ok = fn()
        except Exception as e:
            ok = False
            import traceback
            traceback.print_exc()
            print(f"  exception: {e}")
        print(f"{PASS if ok else FAIL}  {name}")
        if not ok:
            failed += 1
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main_run())
