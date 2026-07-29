"""Meaningful E2E tests for the communicate_mcp chat/inbox-family tools
(mssg, chat, create_chat, read_chat_history, set_chat_sender_policy,
delete_chat), invoked exactly as an LLM would call them, with no model in
the loop, each verifying the actual resulting backend state.

`mssg`/`stop_turn` go over the real `/api/internal/*` loopback (real
X-Internal-Token, driven via FastAPI TestClient). `chat`/`inbox`-family
tools are pure-python stores with no HTTP hop, so they're driven through
`build_mcp_server(..., local=True)` — the exact in-process "as if an LLM
called this tool" path used by test_inbox_provider_parity.py.

Run with:
    cd backend && .venv/bin/python scripts/test_communicate_chat_and_mssg_mcp_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import sys

import _test_home
_TMP_HOME = _test_home.isolate_installed("bc-test-communicate-chat-mssg-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

import communicate_mcp  # noqa: E402
import inbox_store  # noqa: E402
import main  # noqa: E402
import session_store  # noqa: E402
from scripts.auth_test_helpers import authenticate_client  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _new_session(name: str = "t") -> str:
    return session_store.create_session(
        name=name, model="m", cwd="/tmp", orchestration_mode="native",
    )["id"]


def _tok() -> str:
    return main.coordinator.internal_token


# ------------------------------------------------------------------ mssg (real loopback route)
def test_mssg_default_deposits_into_recipient_inbox(client: TestClient) -> bool:
    sender = _new_session("sender")
    recipient = _new_session("recipient")
    r = client.post(
        "/api/internal/mssg",
        json={
            "sender_session_id": sender,
            "target_session_id": recipient,
            "message": "hello from sender",
        },
        headers={"X-Internal-Token": _tok()},
    )
    if r.status_code != 200 or not r.json().get("success"):
        print(f"  mssg failed: {r.status_code} {r.text}")
        return False
    body = r.json()
    if body.get("queued") is not False or not body.get("delivered"):
        print(f"  mssg should deposit-only by default: {body}")
        return False
    # Independently verify the message actually reached the recipient's inbox,
    # not just that the route returned success.
    history = inbox_store.read_history(recipient_session_id=recipient)
    texts = [m["text"] for m in history["messages"]]
    if "hello from sender" not in texts:
        print(f"  message not found in recipient inbox: {history}")
        return False
    return True


def test_mssg_queue_turn_true_does_not_deposit_to_inbox(client: TestClient) -> bool:
    """queue_turn=True routes through the queue-and-fire-turn path instead of
    the plain inbox deposit. That path ends in a real turn dispatch
    (`coordinator.submit_prompt_async`) which would spawn a real provider
    subprocess, so it's stubbed out here — this test verifies the dispatch
    was actually reached with the right target/message and that a real
    queued-prompt record landed on the target session, without letting any
    model run."""
    sender = _new_session("sender2")
    recipient = _new_session("recipient2")
    calls = []

    async def fake_submit_prompt_async(app_session_id, prompt_params, **kw):
        calls.append((app_session_id, prompt_params))

    original = main.coordinator.submit_prompt_async
    main.coordinator.submit_prompt_async = fake_submit_prompt_async
    try:
        r = client.post(
            "/api/internal/mssg",
            json={
                "sender_session_id": sender,
                "target_session_id": recipient,
                "message": "queued message",
                "queue_turn": True,
            },
            headers={"X-Internal-Token": _tok()},
        )
    finally:
        main.coordinator.submit_prompt_async = original

    if r.status_code != 200 or not r.json().get("success"):
        print(f"  mssg queue_turn=True failed: {r.status_code} {r.text}")
        return False
    if not calls or calls[-1][0] != recipient or calls[-1][1].get("prompt") != "queued message":
        print(f"  dispatch was not reached with the right target/message: {calls}")
        return False
    from session_manager import manager as session_manager

    queued = session_manager.get(recipient).get("queued_prompts") or []
    if not any(item.get("content") == "queued message" for item in queued):
        print(f"  no queued-prompt record landed on the target session: {queued}")
        return False
    history = inbox_store.read_history(recipient_session_id=recipient)
    texts = [m["text"] for m in history["messages"]]
    if "queued message" in texts:
        print("  queue_turn=True should NOT deposit into the inbox store")
        return False
    return True


def test_mssg_missing_message_is_400(client: TestClient) -> bool:
    sender = _new_session("sender3")
    recipient = _new_session("recipient3")
    r = client.post(
        "/api/internal/mssg",
        json={"sender_session_id": sender, "target_session_id": recipient, "message": ""},
        headers={"X-Internal-Token": _tok()},
    )
    if r.status_code != 400:
        print(f"  expected 400 for empty message, got {r.status_code}")
        return False
    return True


# ------------------------------------------------------------------ chat family (pure-python, local MCP call)
def _local_tools():
    from better_agent_sdk.surfaces import build_mcp_server

    server = build_mcp_server(
        "communicate",
        communicate_mcp._specs(),
        instructions=communicate_mcp._INSTRUCTIONS,
        local=True,
    )
    return {tool.name: tool for tool in server._tool_manager.list_tools()}


def _call(tool, **kwargs):
    return asyncio.run(tool.fn(**kwargs))


def _as_sender(sender_session_id: str, fn):
    original = os.environ.get("BETTER_CLAUDE_MSSG_SENDER_SESSION_ID")
    os.environ["BETTER_CLAUDE_MSSG_SENDER_SESSION_ID"] = sender_session_id
    try:
        return fn()
    finally:
        if original is None:
            os.environ.pop("BETTER_CLAUDE_MSSG_SENDER_SESSION_ID", None)
        else:
            os.environ["BETTER_CLAUDE_MSSG_SENDER_SESSION_ID"] = original


def test_chat_create_post_and_read_roundtrip() -> bool:
    a = _new_session("chat-a")
    b = _new_session("chat-b")
    tools = _local_tools()
    chat_id = "room-1"

    created = _as_sender(a, lambda: _call(tools["create_chat"], chat_id=chat_id, name="room"))
    if created.get("id") != chat_id:
        print(f"  create_chat did not return the created chat: {created}")
        return False

    posted = _as_sender(a, lambda: _call(tools["chat"], chat_id=chat_id, message="hi from a"))
    if not posted.get("count") == 0 and posted.get("new_messages"):
        # posting as the owner still returns messages newer than a's own cursor
        pass

    read_by_b = _as_sender(b, lambda: _call(tools["chat"], chat_id=chat_id))
    texts = [m["text"] for m in read_by_b.get("new_messages", [])]
    if "hi from a" not in texts:
        print(f"  b did not see a's message via chat(): {read_by_b}")
        return False

    history = _as_sender(b, lambda: _call(tools["read_chat_history"], chat_id=chat_id))
    history_texts = [m["text"] for m in history.get("messages", [])]
    if "hi from a" not in history_texts:
        print(f"  read_chat_history missing message: {history}")
        return False
    return True


def test_set_chat_sender_policy_blocks_disallowed_sender() -> bool:
    owner = _new_session("chat-owner")
    outsider = _new_session("chat-outsider")
    tools = _local_tools()
    chat_id = "room-2"
    _as_sender(owner, lambda: _call(tools["create_chat"], chat_id=chat_id))

    policy = _as_sender(
        owner,
        lambda: _call(
            tools["set_chat_sender_policy"],
            chat_id=chat_id,
            sender_policy="allowlist",
            sender_ids=[owner],
        ),
    )
    if policy.get("sender_policy") != "allowlist":
        print(f"  policy not applied: {policy}")
        return False

    blocked = _as_sender(outsider, lambda: _call(tools["chat"], chat_id=chat_id, message="sneaky"))
    if not blocked.get("success") is False and "error" not in blocked:
        print(f"  outsider post should be rejected: {blocked}")
        return False

    # Independently verify the message never landed in chat history.
    history = _as_sender(owner, lambda: _call(tools["read_chat_history"], chat_id=chat_id))
    texts = [m["text"] for m in history.get("messages", [])]
    if "sneaky" in texts:
        print(f"  disallowed sender's message was persisted: {history}")
        return False

    allowed = _as_sender(owner, lambda: _call(tools["chat"], chat_id=chat_id, message="from owner"))
    if allowed.get("success") is False:
        print(f"  owner post should always be allowed: {allowed}")
        return False
    return True


def test_delete_chat_removes_it() -> bool:
    owner = _new_session("chat-owner-2")
    tools = _local_tools()
    chat_id = "room-3"
    _as_sender(owner, lambda: _call(tools["create_chat"], chat_id=chat_id))
    result = _as_sender(owner, lambda: _call(tools["delete_chat"], chat_id=chat_id))
    if result is not True and not (isinstance(result, dict) and result.get("deleted")):
        print(f"  delete_chat did not report deletion: {result}")
        return False
    # Independently verify: reading history for a deleted chat must fail/error.
    after = _as_sender(owner, lambda: _call(tools["read_chat_history"], chat_id=chat_id))
    if isinstance(after, dict) and after.get("success") is not False:
        print(f"  chat still readable after delete_chat: {after}")
        return False
    return True


TESTS_HTTP = [
    ("mssg default deposits into recipient inbox", test_mssg_default_deposits_into_recipient_inbox),
    ("mssg queue_turn=True does not deposit to inbox", test_mssg_queue_turn_true_does_not_deposit_to_inbox),
    ("mssg missing message -> 400", test_mssg_missing_message_is_400),
]

TESTS_LOCAL = [
    ("chat create/post/read roundtrip", test_chat_create_post_and_read_roundtrip),
    ("set_chat_sender_policy blocks disallowed sender", test_set_chat_sender_policy_blocks_disallowed_sender),
    ("delete_chat removes it", test_delete_chat_removes_it),
]


def main_run() -> int:
    failed = 0
    with TestClient(main.app, client=("127.0.0.1", 50000)) as client:
        authenticate_client(client)
        for name, fn in TESTS_HTTP:
            try:
                ok = fn(client)
            except Exception as e:
                ok = False
                import traceback
                traceback.print_exc()
                print(f"  exception: {e}")
            print(f"{PASS if ok else FAIL}  {name}")
            if not ok:
                failed += 1
    for name, fn in TESTS_LOCAL:
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
    total = len(TESTS_HTTP) + len(TESTS_LOCAL)
    print()
    if failed:
        print(f"{failed} of {total} test(s) FAILED")
    else:
        print(f"all {total} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
