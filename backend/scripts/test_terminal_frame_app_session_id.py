"""Locks: every terminal WS frame (turn_complete / turn_stopped /
turn_detached / error) emitted by `run_turn` carries
`data.app_session_id` == the turn's session.

Why this matters: the frontend WS subscribes to EVERY open pane. The
terminal handlers must route the "stop streaming" signal to the pane
the turn belongs to, not the focused pane. They can only do that if the
frame names its session. Before the fix the frames omitted
`app_session_id`, so a turn finishing in a background pane cleared the
WRONG pane and the real one stayed stuck "Running…" until a refresh.

Runs a real native claude turn in-process (slow; needs claude auth),
matching the repo's integration-test convention.

Run with:
    cd backend && .venv/bin/python scripts/test_terminal_frame_app_session_id.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-terminal-asid-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_MODEL = "claude-sonnet-4-20250514"


def _assert_all_terminal_emits_stamped() -> bool:
    """Static lock: every terminal frame `run_turn` emits
    (turn_complete x2, turn_stopped, turn_detached, error) carries
    `app_session_id` in its data dict.

    Deterministic + no subprocess — covers the three terminal types the
    real-turn happy path never produces (stopped / detached / error).
    """
    import re

    src = open(os.path.join(_BACKEND, "orchestrator.py")).read()
    ok = True

    # turn_* frames: assert EVERY emit of these types stamps the field.
    for kind in ("turn_complete", "turn_stopped", "turn_detached"):
        matches = re.findall(
            r'"type":\s*"' + kind + r'"\s*,\s*"data":\s*\{([^}]*)',
            src,
        )
        if not matches:
            print(f"  {FAIL} no emit found for {kind}")
            ok = False
            continue
        for body in matches:
            good = "app_session_id" in body
            ok = ok and good
            print(f"  {PASS if good else FAIL} {kind} emit stamps app_session_id")

    # run_turn's error path (distinct from pre-turn validation errors).
    err_ok = '{"app_session_id": persist_to, "error": error_text}' in src
    ok = ok and err_ok
    print(f"  {PASS if err_ok else FAIL} run_turn error emit stamps app_session_id")
    return ok


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _run() -> bool:
    port = _free_port()
    os.environ["BETTER_CLAUDE_BACKEND_URL"] = f"http://127.0.0.1:{port}"

    import logging
    import uvicorn
    import main
    from session_manager import manager as session_manager

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.ERROR)
    server = uvicorn.Server(uvicorn.Config(
        main.app, host="127.0.0.1", port=port,
        log_level="error", access_log=False, lifespan="off",
    ))
    serve_task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)

    cwd = _TMP_HOME
    session = session_manager.create(
        name="t", model=_MODEL, cwd=cwd,
        orchestration_mode="native", source="cli",
    )
    sid = session["id"]

    terminals: list[tuple[str, object]] = []

    async def ws_callback(event: dict) -> None:
        et = event.get("type", "")
        if et in ("turn_complete", "turn_stopped", "turn_detached", "error"):
            d = event.get("data", {}) if isinstance(event.get("data"), dict) else {}
            terminals.append((et, d.get("app_session_id")))

    main.coordinator.register_ws(sid, ws_callback)
    await asyncio.sleep(0.2)

    await main.coordinator.handle_prompt(
        prompt="reply with only the word hi", app_session_id=sid,
        model=_MODEL, cwd=cwd, ws_callback=ws_callback,
        images=None, orchestration_mode="native",
    )
    await asyncio.sleep(1.0)

    server.should_exit = True
    try:
        await asyncio.wait_for(serve_task, timeout=5)
    except Exception:
        serve_task.cancel()

    ok = True
    if not terminals:
        print(f"{FAIL} no terminal frames captured")
        return False
    for et, asid in terminals:
        good = asid == sid
        ok = ok and good
        print(f"  {PASS if good else FAIL} {et}: app_session_id={asid!r}")
    print(f"\nsession id = {sid}")
    return ok


def main() -> int:
    print("Static: all terminal emit sites stamp app_session_id")
    static_ok = _assert_all_terminal_emits_stamped()

    print("\nLive: real native turn emits turn_complete with app_session_id")
    try:
        live_ok = asyncio.run(_run())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)

    ok = static_ok and live_ok
    print(f"\n{PASS if ok else FAIL} terminal frames carry app_session_id")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
