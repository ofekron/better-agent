"""`ui` MCP server against real agents.

Three tools, three different kinds of backend-owned proof:

* `open_file_panel` — the internal route drives
  `session_manager.add_open_file_panel`, so the session record either lists the
  panel or the model never called the tool.
* `request_user_input` / `request_user_approval` — both BLOCK the runner until
  a human answers, so each case runs a responder that polls
  `GET /api/user-input/pending` and resolves through the real REST route. The
  proof is the resolved record in `user_input_store`, plus the fact that the
  turn only completes once the answer lands.

No model text is parsed anywhere.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import urllib.error
import urllib.request

import _live_agent
from _live_agent import Case, require_cli

SERVER = "ui"
VENDORS = _live_agent.vendors_for_server(SERVER)


def _panel_prompt(path: str) -> str:
    return (
        "This is an automated integration test of Better Agent tool injection. "
        "Call the MCP tool named open_file_panel from the 'ui' server exactly "
        f"once, with mode='panel', path={path!r}, start_line=1, end_line=1. "
        "Do not call any other tool. After the tool returns, reply with the "
        "single word: done"
    )


async def _open_file_panel(vendor, backend, cwd):
    require_cli(vendor)
    from session_manager import manager as session_manager

    target = cwd / f"{vendor.kind}-panel.txt"
    target.write_text("live open_file_panel probe\n", encoding="utf-8")

    sid = backend.new_session(vendor, f"ui/{vendor.kind}", str(cwd))
    await backend.run_turn(
        vendor, sid=sid, prompt=_panel_prompt(str(target)), cwd=str(cwd)
    )

    panels = (session_manager.get(sid) or {}).get("open_file_panels") or []
    if not any(panel.get("path") == str(target) for panel in panels):
        raise AssertionError(
            f"open_file_panel never reached the backend; panels={panels}"
        )


import auth

_TOKEN = auth.create_token("test-user")


def _get(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {_TOKEN}"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=10.0) as response:
        return json.loads(response.read() or b"{}")


def _post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_TOKEN}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15.0) as response:
        return json.loads(response.read() or b"{}")


def _answer_body(record: dict) -> dict:
    """Resolve payload for one pending ask.

    The route matches answers against `questions[].id` and rejects a missing
    or empty answer, so the body is built from the record the agent actually
    produced rather than from a guess about its question text.
    """
    body = {"app_session_id": record.get("app_session_id")}
    if record.get("kind") == "approval":
        return {**body, "approved": True}
    questions = record.get("questions") or []
    if not questions:
        raise AssertionError(f"input request carried no questions: {record}")
    return {
        **body,
        "answers": {str(q.get("id")): "blue" for q in questions},
    }


async def _respond_to_first_ask(backend, sid: str, deadline_s: float) -> dict:
    """Poll for the agent's pending ask and answer it through the real route.

    The runner is blocked inside the MCP call while this runs, so polling here
    is the test standing in for a human, not a workaround for a race.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_s
    while loop.time() < deadline:
        try:
            pending = _get(f"{backend.url}/api/user-input/pending?app_session_id={sid}")
        except urllib.error.URLError:
            await asyncio.sleep(0.5)
            continue
        requests = pending.get("requests") or []
        if requests:
            record = requests[0]
            request_id = str(record.get("request_id") or "")
            if request_id:
                _post(
                    f"{backend.url}/api/user-input/{request_id}/resolve",
                    _answer_body(record),
                )
                return record
        await asyncio.sleep(0.5)
    raise AssertionError(
        f"no pending user-input request appeared within {deadline_s}s — the "
        "agent never called the tool"
    )


async def _ask_case(vendor, backend, cwd, *, tool: str, prompt: str):
    require_cli(vendor)
    import user_input_store

    sid = backend.new_session(vendor, f"ui/{tool}/{vendor.kind}", str(cwd))
    responder = asyncio.create_task(
        _respond_to_first_ask(backend, sid, deadline_s=240.0)
    )
    turn_error: BaseException | None = None
    try:
        await backend.run_turn(vendor, sid=sid, prompt=prompt, cwd=str(cwd))
    except BaseException as exc:  # noqa: BLE001 — re-raised below, after cleanup
        turn_error = exc

    if not responder.done():
        # The turn ended without the agent ever asking, so nothing will answer
        # the responder. Cancelling it must not become the reported failure.
        responder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await responder
        if turn_error is not None:
            raise turn_error
        raise AssertionError(
            f"{tool}: the turn completed without the agent calling the tool"
        )

    record = responder.result()
    if turn_error is not None:
        raise turn_error
    request_id = str(record.get("request_id") or "")
    stored = user_input_store.get_request(request_id)
    if not stored:
        raise AssertionError(f"{tool}: request {request_id} vanished from the store")
    if str(stored.get("status") or "") != "resolved":
        raise AssertionError(
            f"{tool}: request {request_id} is {stored.get('status')!r}, not resolved"
        )


def _input_prompt() -> str:
    return (
        "This is an automated integration test. Call the MCP tool named "
        "request_user_input from the 'ui' server exactly once, asking the "
        "single question 'favourite colour?'. Wait for the answer, then reply "
        "with the single word: done"
    )


def _approval_prompt() -> str:
    return (
        "This is an automated integration test. Call the MCP tool named "
        "request_user_approval from the 'ui' server exactly once, requesting "
        "approval for the action 'run the integration probe'. Wait for the "
        "decision, then reply with the single word: done"
    )


def cases() -> list[Case]:
    out: list[Case] = []
    for vendor in VENDORS:
        out.append(Case(SERVER, "open_file_panel", vendor, _open_file_panel))
        out.append(
            Case(
                SERVER,
                "request_user_input",
                vendor,
                lambda v, b, c: _ask_case(
                    v, b, c, tool="request_user_input", prompt=_input_prompt()
                ),
            )
        )
        out.append(
            Case(
                SERVER,
                "request_user_approval",
                vendor,
                lambda v, b, c: _ask_case(
                    v, b, c, tool="request_user_approval", prompt=_approval_prompt()
                ),
            )
        )
    return out


__all__ = ["cases"]
