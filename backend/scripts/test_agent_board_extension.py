#!/usr/bin/env python3
"""Agent Board extension: registration, the run-prompt identity gate
(fail-closed), and the board engine's drop-creates/moves-card behavior."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP_HOME = Path(tempfile.mkdtemp(prefix="bc-test-agent-board-"))
import _test_home

_test_home.isolate("ba-test-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"
# Isolate the agent-board data home so the dispatched extension backend never
# touches the developer's real ~/.agent-board during the test.
os.environ["AGENT_BOARD_HOME"] = str(TMP_HOME / "agent-board-home")
os.environ.setdefault("AGENT_BOARD_REPO", str(Path.home() / "agent-board"))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

dist_dir = ROOT.parent / "frontend" / "dist"
created_dist = not dist_dir.exists()
if created_dist:
    dist_dir.mkdir(parents=True, exist_ok=True)
    (dist_dir / "index.html").write_text("<!doctype html><title>stub</title>", encoding="utf-8")

from fastapi.testclient import TestClient  # noqa: E402

import extension_store  # noqa: E402
import extension_token_registry  # noqa: E402
import main  # noqa: E402
import auth  # noqa: E402

AGENT_BOARD_ID = extension_store.BUILTIN_AGENT_BOARD_EXTENSION_ID


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def test_registration() -> None:
    check(AGENT_BOARD_ID == extension_store.BUILTIN_AGENT_BOARD_EXTENSION_ID, "agent-board id constant")
    check(
        extension_store._PRIVATE_EXTENSION_PATHS.get(AGENT_BOARD_ID) == "extensions/agent-board",
        "agent-board registered in private path table",
    )
    check(
        extension_store._PRIVATE_EXTENSION_NAMES.get(AGENT_BOARD_ID) == "Agent Board",
        "agent-board registered in private name table",
    )


def test_installed_and_exposed(client: TestClient) -> None:
    import json

    client.get("/api/extensions")  # triggers reconcile + local install
    check(
        extension_store.is_extension_active(AGENT_BOARD_ID),
        "agent-board is installed and active (first-party, consent-exempt)",
    )
    entrypoints = json.dumps(client.get("/api/extensions/frontend-entrypoints").json())
    check(
        "session-drag-overlay" in entrypoints and AGENT_BOARD_ID in entrypoints,
        "agent-board exposes the session-drag-overlay frontend module",
    )
    health = client.get(f"/api/extensions/{AGENT_BOARD_ID}/backend/health")
    check(health.status_code == 200 and health.json().get("ok") is True, "backend /health dispatches")
    repo = Path(os.environ.get("AGENT_BOARD_REPO") or "")
    if (repo / "backend" / "board_store.py").exists():
        board = client.get(f"/api/extensions/{AGENT_BOARD_ID}/backend/board")
        check(board.status_code == 200, "backend /board dispatches into the agent-board engine")
        check(len(board.json()["board"]["columns"]) >= 2, "board has default lanes")
    else:
        print("SKIP /board dispatch — agent-board project not found")


def test_run_prompt_identity_gate(client: TestClient) -> None:
    async def _stub_run(target_sid, prompt, *, source):
        return {"session_id": target_sid}

    core_token = getattr(main.coordinator, "internal_token", "")
    created = client.post(
        "/api/internal/create-session",
        headers={"X-Internal-Token": core_token},
        json={"name": "board target", "cwd": str(TMP_HOME)},
    )
    check(created.status_code == 200, "core create-session for a real target")
    real_sid = created.json()["session_id"]

    original = main.session_bridge.run_for_extension
    main.session_bridge.run_for_extension = _stub_run
    try:
        ab_headers = {"X-Internal-Token": extension_token_registry.mint(AGENT_BOARD_ID)}

        # Wrong identity: another extension's token must NOT reach the endpoint.
        response = client.post(
            "/api/internal/agent-board/run-prompt",
            headers={"X-Internal-Token": extension_token_registry.mint("ofek-dev.ask")},
            json={"session_id": real_sid, "prompt": "hi"},
        )
        check(response.status_code == 403, "run-prompt rejects non-agent-board identity")

        # No token at all → 403/422.
        response = client.post(
            "/api/internal/agent-board/run-prompt",
            json={"session_id": real_sid, "prompt": "hi"},
        )
        check(response.status_code in (403, 422), "run-prompt rejects missing token")

        # Correct identity but empty body → 400 (fail closed on bad input).
        response = client.post(
            "/api/internal/agent-board/run-prompt",
            headers=ab_headers,
            json={"session_id": "", "prompt": ""},
        )
        check(response.status_code == 400, "run-prompt rejects empty session/prompt")

        # Unknown session id → 404 (cannot drive arbitrary/nonexistent sessions).
        response = client.post(
            "/api/internal/agent-board/run-prompt",
            headers=ab_headers,
            json={"session_id": "does-not-exist", "prompt": "do it"},
        )
        check(response.status_code == 404, "run-prompt rejects unknown session id")

        # Over-long prompt → 400 (endpoint-level cap, not just lane-action cap).
        response = client.post(
            "/api/internal/agent-board/run-prompt",
            headers=ab_headers,
            json={"session_id": real_sid, "prompt": "x" * 9000},
        )
        check(response.status_code == 400, "run-prompt rejects over-long prompt")

        # Correct identity + real session + valid prompt → scheduled.
        response = client.post(
            "/api/internal/agent-board/run-prompt",
            headers=ab_headers,
            json={"session_id": real_sid, "prompt": "do it"},
        )
        check(response.status_code == 200, "run-prompt accepts agent-board identity + real session")
        check(response.json().get("scheduled") is True, "run-prompt schedules delivery")

        # Busy target → 409 synchronously (no silent drop of the prompt).
        orig_busy = main.coordinator.turn_manager.has_active_runs
        main.coordinator.turn_manager.has_active_runs = lambda sid: sid == real_sid
        try:
            response = client.post(
                "/api/internal/agent-board/run-prompt",
                headers=ab_headers,
                json={"session_id": real_sid, "prompt": "do it"},
            )
            check(response.status_code == 409, "run-prompt rejects busy target session")
        finally:
            main.coordinator.turn_manager.has_active_runs = orig_busy
    finally:
        main.session_bridge.run_for_extension = original


# The engine reuses the agent-board project, which uses bare top-level imports
# (`from models import ...`). Run it in a CLEAN subprocess so its `models`/
# `board_store` modules resolve to agent-board — exactly as in the real
# extension backend subprocess, not shadowed by better-claude's own modules.
_ENGINE_DRIVER = r'''
import os, sys
ext_backend, board_backend = sys.argv[1], sys.argv[2]
sys.path.insert(0, board_backend)
sys.path.insert(0, ext_backend)
import engine

def check(cond, msg):
    if not cond:
        print("FAIL " + msg); sys.exit(1)
    print("PASS " + msg)

snap = engine.board_snapshot()
lanes = snap["board"]["columns"]
check(len(lanes) >= 2, "default board has lanes")
backlog, second = lanes[0]["id"], lanes[1]["id"]

res = engine.drop_session("sess-abc", "My Session", backlog)
card_id = res["card"]["id"]
check(res["card"]["column_id"] == backlog, "drop places card in target lane")
snap = engine.board_snapshot()
mine = [c for c in snap["cards"] if c["session_id"] == "sess-abc"]
check(len(mine) == 1 and mine[0]["id"] == card_id, "drop creates exactly one session card")

res2 = engine.drop_session("sess-abc", "My Session", second)
check(res2["card"]["id"] == card_id, "re-drop reuses the same card")
check(res2["card"]["column_id"] == second, "re-drop moves card to new lane")
snap = engine.board_snapshot()
mine = [c for c in snap["cards"] if c["session_id"] == "sess-abc"]
check(len(mine) == 1, "re-drop does not duplicate the card")

for bad in [{"type": "delete"}, {"type": "prompt"}, {"type": "prompt", "prompt": "  "}, "x", {}]:
    try:
        engine.validate_action(bad)
        check(False, "validate_action rejects " + repr(bad))
    except ValueError:
        check(True, "validate_action rejects " + repr(bad))

saved = engine.set_lane_action(second, {"type": "prompt", "prompt": "go"})
check(saved == {"type": "prompt", "prompt": "go"}, "set prompt action persists")
snap = engine.board_snapshot()
check(snap["lane_actions"].get(second, {}).get("type") == "prompt", "lane action in snapshot")
drop3 = engine.drop_session("sess-xyz", "Other", second)
check(drop3["action"]["type"] == "prompt", "drop returns the lane's prompt action")
print("ENGINE_OK")
'''


def test_engine_drop_creates_and_moves_card() -> None:
    import subprocess

    repo = Path(os.environ.get("AGENT_BOARD_REPO") or (Path.home() / "agent-board"))
    if not (repo / "backend" / "board_store.py").exists():
        print(f"SKIP engine tests — agent-board project not found at {repo}")
        return
    ext_backend = ROOT.parent / "better-agent-private" / "extensions" / "agent-board" / "backend"
    check(ext_backend.exists(), "extension backend dir present")

    env = dict(os.environ)
    env["AGENT_BOARD_REPO"] = str(repo)
    env["AGENT_BOARD_HOME"] = str(TMP_HOME / "agent-board-home")
    proc = subprocess.run(
        [sys.executable, "-c", _ENGINE_DRIVER, str(ext_backend), str(repo / "backend")],
        env=env,
        capture_output=True,
        text=True,
    )
    print(proc.stdout, end="")
    if proc.returncode != 0 or "ENGINE_OK" not in proc.stdout:
        raise AssertionError(f"engine subprocess failed:\n{proc.stdout}\n{proc.stderr}")


if __name__ == "__main__":
    try:
        test_registration()
        with TestClient(main.app) as client:
            client.headers.update({"Authorization": f"Bearer {auth.create_token('test')}"})
            test_installed_and_exposed(client)
            test_run_prompt_identity_gate(client)
        test_engine_drop_creates_and_moves_card()
        print("\nALL AGENT BOARD TESTS PASSED")
    finally:
        if created_dist:
            shutil.rmtree(dist_dir, ignore_errors=True)
        shutil.rmtree(TMP_HOME, ignore_errors=True)
