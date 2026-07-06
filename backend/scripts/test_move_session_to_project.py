#!/usr/bin/env python3
"""Move-session-to-project: endpoint state transitions + turn-1 continuation wrap.

Locks:
  1. POST /api/sessions/{sid}/move-to-project creates a new session in the
     target cwd, seeds its continuation_chain with the old session's chain +
     provider-native agent sid, stamps moved_from/moved_to pointers, and
     archives the old session.
  2. Validation fails closed: missing/nonexistent cwd, same-project move,
     double move, unknown session.
  3. A never-ran session moves cleanly with an empty chain.
  4. _drive_cli_run wraps the FIRST prompt of a moved session with the
     moved_project continuation handoff (chain passed to the provider,
     wrap happens exactly once), and leaves non-moved sessions untouched.

Run with:
    cd backend && .venv/bin/python scripts/test_move_session_to_project.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _test_home
_test_home.isolate("ba-test-move-session-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from auth_test_helpers import authenticate_client  # noqa: E402
import main  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from turn_manager import TurnManager  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool) -> None:
    print(("  PASS" if ok else "  FAIL") + f": {name}")
    if not ok:
        failures.append(name)


def _mkproj(tag: str) -> str:
    return os.path.realpath(tempfile.mkdtemp(prefix=f"ba-move-{tag}-"))


def test_move_endpoint_state(client: TestClient) -> None:
    print("T1 move endpoint state transitions")
    proj_a, proj_b = _mkproj("a"), _mkproj("b")
    old = session_manager.create(
        name="My work", cwd=proj_a, model="sonnet", user_initiated=True,
    )
    sid = old["id"]
    session_manager.set_continuation_chain(sid, ["prov-old-1"])
    session_manager.set_agent_sid(sid, "native", "prov-old-2")

    r = client.post(f"/api/sessions/{sid}/move-to-project", json={"cwd": proj_b})
    check("move returns 200", r.status_code == 200)
    new = r.json()
    new_sid = new["id"]
    check("new session is a different session", new_sid != sid)
    check("new session lives in target cwd", os.path.realpath(new["cwd"]) == proj_b)

    new_rec = session_manager.get(new_sid) or {}
    old_rec = session_manager.get(sid) or {}
    check(
        "chain seeded with old chain + agent sid",
        new_rec.get("continuation_chain") == ["prov-old-1", "prov-old-2"],
    )
    check("moved_from stamped on new", new_rec.get("moved_from_session_id") == sid)
    check("moved_to stamped on old", old_rec.get("moved_to_session_id") == new_sid)
    check("old session archived", old_rec.get("archived") is True)
    check("selectors copied", new_rec.get("model") == old_rec.get("model"))

    r2 = client.post(f"/api/sessions/{sid}/move-to-project", json={"cwd": _mkproj("c")})
    check("second move rejected 409", r2.status_code == 409)


def test_move_endpoint_validation(client: TestClient) -> None:
    print("T2 move endpoint fails closed")
    proj = _mkproj("v")
    sid = session_manager.create(name="v", cwd=proj, model="sonnet")["id"]

    r = client.post(f"/api/sessions/{sid}/move-to-project", json={})
    check("missing cwd rejected 400", r.status_code == 400)
    r = client.post(
        f"/api/sessions/{sid}/move-to-project",
        json={"cwd": "/definitely/not/a/dir/zzz"},
    )
    check("nonexistent cwd rejected 400", r.status_code == 400)
    r = client.post(f"/api/sessions/{sid}/move-to-project", json={"cwd": proj})
    check("same-project move rejected 400", r.status_code == 400)
    r = client.post(
        "/api/sessions/no-such-session/move-to-project", json={"cwd": proj},
    )
    check("unknown session rejected 404", r.status_code == 404)


def test_move_never_ran_session(client: TestClient) -> None:
    print("T3 never-ran session moves with empty chain")
    sid = session_manager.create(name="fresh", cwd=_mkproj("f1"), model="sonnet")["id"]
    r = client.post(
        f"/api/sessions/{sid}/move-to-project", json={"cwd": _mkproj("f2")},
    )
    check("move returns 200", r.status_code == 200)
    new_rec = session_manager.get(r.json()["id"]) or {}
    check("chain stays empty", not new_rec.get("continuation_chain"))
    check("moved_from stamped", new_rec.get("moved_from_session_id") == sid)


class _StubCoordinator:
    def __init__(self, provider) -> None:
        self._in_flight_prompts: dict[str, int] = {}
        self._prompt_queues: dict = {}
        self._session_cancelled: dict[str, bool] = {}
        self.internal_token = "test-token"
        self._provider = provider

    def provider_for_run(self, sid, provider_id=None):
        return self._provider

    def provider_for_session(self, sid):
        return self._provider

    async def broadcast_session(self, *args, **kwargs) -> None:
        pass


class _UPM:
    @staticmethod
    def get_in_flight_lifecycle_msg_id(sid):
        return None


class _CaptureProvider:
    KIND = "codex"
    id = "codex"
    _runs: dict = {}

    def __init__(self) -> None:
        self.captured: list[dict] = []

    def start_run(self, **kw):
        self.captured.append(kw)
        kw["loop"].call_soon_threadsafe(
            kw["queue"].put_nowait,
            type("E", (), {
                "type": "complete",
                "data": {"success": True, "session_id": None, "token_usage": None},
            })(),
        )

    def is_running(self, _run_id: str) -> bool:
        return False


def _drive_first_turn(sid: str, prompt: str) -> _CaptureProvider:
    provider = _CaptureProvider()
    c = _StubCoordinator(provider)
    c.user_prompt_manager = _UPM()
    tm = TurnManager(c)

    async def _ws(_e):
        pass

    async def _go() -> dict:
        return await tm._drive_cli_run(
            prompt=prompt,
            cwd="/tmp",
            model="sonnet",
            session_id=None,
            ws_callback=_ws,
            app_session_id=sid,
            cancel_event=asyncio.Event(),
            session_id_field="agent_session_id",
            mode="native",
            turn_run_id="turn-move-wrap",
        )

    result = asyncio.run(_go())
    check("drive result success", result.get("success") is True)
    return provider


def test_first_turn_wraps_moved_project_continuation() -> None:
    print("T4 first turn of a moved session gets the moved_project handoff")
    sid = session_manager.create(name="moved", cwd="/tmp", model="sonnet")["id"]
    session_manager.set_moved_from(sid, "source-session-id")
    session_manager.set_continuation_chain(sid, ["prov-moved-1"])

    provider = _drive_first_turn(sid, "continue the refactor")
    check("provider spawned exactly once", len(provider.captured) == 1)
    kw = provider.captured[0]
    check(
        "prompt wrapped with moved_project handoff",
        "moved here from another project" in kw.get("prompt", ""),
    )
    check(
        "original prompt preserved inside handoff",
        "continue the refactor" in kw.get("prompt", ""),
    )
    check(
        "seeded chain forwarded to provider",
        kw.get("continuation_chain") == ["prov-moved-1"],
    )


def test_first_turn_of_normal_session_not_wrapped() -> None:
    print("T5 non-moved session's first prompt is untouched")
    sid = session_manager.create(name="plain", cwd="/tmp", model="sonnet")["id"]
    provider = _drive_first_turn(sid, "just a prompt")
    check("provider spawned exactly once", len(provider.captured) == 1)
    check(
        "prompt passed through verbatim",
        provider.captured[0].get("prompt") == "just a prompt",
    )


def run() -> None:
    with TestClient(main.app) as client:
        authenticate_client(client)
        test_move_endpoint_state(client)
        test_move_endpoint_validation(client)
        test_move_never_ran_session(client)
    test_first_turn_wraps_moved_project_continuation()
    test_first_turn_of_normal_session_not_wrapped()
    if failures:
        print(f"FAILURES: {failures}")
        raise SystemExit(1)
    print("test_move_session_to_project: OK")


if __name__ == "__main__":
    run()
