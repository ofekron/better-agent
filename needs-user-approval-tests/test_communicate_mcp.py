"""`communicate` tool set against real agents.

Only Claude and Gemini expose these as a real MCP server; Codex ships the same
tools as per-turn dynamic tools. Both routes end at the same backend
operations, so the cases assert the operation's durable effect and stay
agnostic about the transport.

Covered here are the tools whose effect is durable and cheap:

* `create_chat` + `chat` — `chat_store` writes a per-chat file; no second live
  session is needed.
* `inbox` — `inbox_store` writes a per-recipient file; needs a target session
  record, not a running agent.
* `mssg` — queues a prompt on a target session, surfaced through
  `GET /api/communications`.

Deliberately excluded: `delegate_task`, `create_session`, `create_sub_session`,
`create_worker` and `ensure_named_worker` all spawn further real agents, which
would multiply the cost of this suite without testing more of the MCP layer.
`stop_turn` is excluded because it aborts the very turn under test.
"""
from __future__ import annotations

import _live_agent
from _live_agent import Case, require_cli, tool_prompt

SERVER = "communicate"
VENDORS = _live_agent.vendors_for_server(SERVER)


def _chat_prompt(chat_id: str, message: str) -> str:
    return tool_prompt(
        SERVER,
        "chat",
        f"First call create_chat with chat_id={chat_id!r}, then call chat with "
        f"chat_id={chat_id!r} and message={message!r}.",
    )


async def _chat(vendor, backend, cwd):
    require_cli(vendor)
    import chat_store

    chat_id = f"live-{vendor.kind}-chat"
    message = f"probe from {vendor.kind}"
    sid = backend.new_session(vendor, f"communicate/chat/{vendor.kind}", str(cwd))
    await backend.run_turn(
        vendor, sid=sid, prompt=_chat_prompt(chat_id, message), cwd=str(cwd)
    )

    messages = chat_store.read_history(chat_id=chat_id).get("messages") or []
    if not messages:
        raise AssertionError(f"chat {chat_id!r} has no messages after the turn")
    if not any(message in str(entry.get("text") or "") for entry in messages):
        raise AssertionError(
            f"chat {chat_id!r} never received {message!r}; got {messages}"
        )


def _inbox_prompt(recipient_sid: str, message: str) -> str:
    return tool_prompt(
        SERVER,
        "inbox",
        f"Use recipient_session_id={recipient_sid!r} and message={message!r}.",
    )


async def _inbox(vendor, backend, cwd):
    require_cli(vendor)
    import inbox_store

    recipient = backend.new_session(
        vendor, f"communicate/inbox-target/{vendor.kind}", str(cwd)
    )
    sender = backend.new_session(
        vendor, f"communicate/inbox/{vendor.kind}", str(cwd)
    )
    message = f"inbox probe from {vendor.kind}"
    await backend.run_turn(
        vendor, sid=sender, prompt=_inbox_prompt(recipient, message), cwd=str(cwd)
    )

    entries = inbox_store.read_history(
        recipient_session_id=recipient
    ).get("messages") or []
    if not entries:
        raise AssertionError(f"inbox for {recipient} is empty after the turn")
    if not any(message in str(entry.get("text") or "") for entry in entries):
        raise AssertionError(
            f"inbox for {recipient} never received {message!r}; got {entries}"
        )


def _mssg_prompt(target_sid: str, message: str) -> str:
    return tool_prompt(
        SERVER,
        "mssg",
        f"Use target_session_id={target_sid!r} and message={message!r}.",
    )


async def _mssg(vendor, backend, cwd):
    require_cli(vendor)
    import communication_log

    target = backend.new_session(
        vendor, f"communicate/mssg-target/{vendor.kind}", str(cwd)
    )
    sender = backend.new_session(vendor, f"communicate/mssg/{vendor.kind}", str(cwd))
    message = f"mssg probe from {vendor.kind}"
    await backend.run_turn(
        vendor, sid=sender, prompt=_mssg_prompt(target, message), cwd=str(cwd)
    )

    rows = communication_log.list_communications(
        session_id=target, limit=200
    ).get("items") or []
    if not rows:
        raise AssertionError(f"target session {target} logged no communications")
    if not any(message in str(row.get("body") or "") for row in rows):
        raise AssertionError(
            f"target session {target} never received {message!r}; got {rows}"
        )


def cases() -> list[Case]:
    out: list[Case] = []
    for vendor in VENDORS:
        out.append(Case(SERVER, "create_chat+chat", vendor, _chat))
        out.append(Case(SERVER, "inbox", vendor, _inbox))
        out.append(Case(SERVER, "mssg", vendor, _mssg))
    return out


__all__ = ["cases"]
