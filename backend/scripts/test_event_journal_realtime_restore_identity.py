"""Prove realtime ownership projection equals cold restore.

Run with:
    cd backend && .venv/bin/python scripts/test_event_journal_realtime_restore_identity.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-journal-realtime-restore-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_bus import BusEvent, EventBus  # noqa: E402
from event_bus_subscribers import _refresh_session_content_projection  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
from event_journal import (  # noqa: E402
    EVENT_JOURNAL_WRITTEN,
    EventJournalWriter,
    bind_event_journal_loop,
    publish_event,
)
from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from session_ws_broadcaster import SessionWSBroadcaster  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _assistant(message_id: str) -> dict:
    msg = get_strategy("native").build_assistant_scaffold()
    msg["id"] = message_id
    msg["isStreaming"] = False
    return msg


def _fingerprint(session: dict) -> dict[str, tuple[list[str], str]]:
    out: dict[str, tuple[list[str], str]] = {}
    for msg in session.get("messages") or []:
        if msg.get("role") != "assistant":
            continue
        uuids = [
            (event.get("data") or {}).get("uuid")
            for event in msg.get("events") or []
            if (event.get("data") or {}).get("uuid")
        ]
        out[msg["id"]] = (uuids, msg.get("content") or "")
    return out


def _expanded_fingerprint(sid: str, message_ids: list[str]) -> dict[str, tuple[list[str], str]]:
    return _fingerprint({
        "messages": [
            msg
            for message_id in message_ids
            if (msg := session_manager.get_message_full(sid, message_id)) is not None
        ],
    })


async def _run() -> bool:
    loop = asyncio.get_running_loop()
    bind_event_journal_loop(loop)
    bus = EventBus()
    writer = EventJournalWriter()
    writer.register(bus)
    bus.subscribe(
        EVENT_JOURNAL_WRITTEN,
        _refresh_session_content_projection,
        priority=20,
        name="identity-session-projection",
    )

    session = session_manager.create(
        name="identity", cwd="/tmp", orchestration_mode="native",
    )
    sid = session["id"]
    session_manager.append_assistant_msg(sid, _assistant("msg-1"))
    session_manager.append_assistant_msg(sid, _assistant("msg-2"))

    changes: list[dict] = []
    session_manager.add_listener(
        lambda changed_sid, change: (
            changes.append(change)
            if changed_sid == sid and change.get("kind") == "message_ownership_resolved"
            else None
        ),
    )

    async def publish(*, context_id: str = sid, **kwargs):
        return await publish_event(
            session_id=sid,
            context_id=context_id,
            source="identity-test",
            bus_instance=bus,
            **kwargs,
        )

    await publish(
        event_type="turn_started",
        data={
            "turn_id": "turn-1",
            "message_id": "msg-1",
            "source_ts": "2026-06-06T10:00:00Z",
        },
        message_id="msg-1",
        turn_id="turn-1",
    )
    await publish(
        event_type="turn_started",
        data={
            "turn_id": "turn-2",
            "message_id": "msg-2",
            "source_ts": "2026-06-06T11:00:00Z",
        },
        message_id="msg-2",
        turn_id="turn-2",
    )
    unresolved = await publish(
        event_type="agent_message",
        data={
            "uuid": "late-child",
            "parentUuid": "late-parent",
            "isSidechain": True,
            "timestamp": "2026-06-06T11:10:00Z",
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "late child text"}],
            },
        },
    )
    await publish(
        event_type="agent_message",
        data={
            "uuid": "late-parent",
            "type": "assistant",
            "message": {"role": "assistant", "content": []},
        },
        message_id="msg-1",
    )
    await publish(
        context_id="worker-context-not-a-session",
        event_type="agent_message",
        data={
            "uuid": "worker-late-child",
            "parentUuid": "worker-late-parent",
            "isSidechain": True,
            "timestamp": "2026-06-06T11:20:00Z",
        },
    )
    await publish(
        event_type="agent_message",
        data={"uuid": "worker-late-parent"},
        message_id="msg-2",
    )
    for _ in range(100):
        live = _fingerprint(session_manager.get(sid) or {})
        if (
            "late-child" in live.get("msg-1", ([], ""))[0]
            and "worker-late-child" in live.get("msg-2", ([], ""))[0]
        ):
            break
        await asyncio.sleep(0.01)
    live = _fingerprint(session_manager.get(sid) or {})

    root_ref = session_manager.get_ref(sid) or {}
    msg1 = next(
        (msg for msg in root_ref.get("messages") or []
         if msg.get("id") == "msg-1"),
        None,
    )
    if msg1 is not None:
        get_strategy("native").apply_event(
            app_session_id=sid,
            msg=msg1,
            event={
                "type": "agent_message",
                "data": {
                    "uuid": "late-child",
                    "parentUuid": "late-parent",
                    "isSidechain": True,
                    "timestamp": "2026-06-06T11:30:00Z",
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "late child updated"}
                        ],
                    },
                },
            },
            ctx=ApplyEventCtx(root_id=sid),
            source_is_provider_stream=False,
        )
    post_update = _fingerprint(session_manager.get(sid) or {})
    post_update_session = session_manager.get(sid) or {}
    post_update_msg1 = next(
        (msg for msg in post_update_session.get("messages") or []
         if msg.get("id") == "msg-1"),
        {},
    )
    post_update_events = post_update_msg1.get("events") or []
    post_update_child_text = ""
    if post_update_events:
        content = ((post_update_events[0].get("data") or {})
                   .get("message") or {}).get("content")
        if isinstance(content, list) and content:
            post_update_child_text = content[0].get("text") or ""

    session_manager.flush_pending_persists()
    session_manager._roots.pop(sid, None)
    session_manager._event_hydrated_roots.discard(sid)
    event_ingester.close_all()
    restored = _expanded_fingerprint(sid, ["msg-1", "msg-2"])

    ok_live = (
        unresolved.msg_id is None
        and "late-child" in live.get("msg-1", ([], ""))[0]
        and "late-child" not in live.get("msg-2", ([], ""))[0]
        and "worker-late-child" in live.get("msg-2", ([], ""))[0]
        and bool(changes)
    )
    ok_identity = post_update == restored
    ok_replacement = (
        post_update.get("msg-1", ([], ""))[0] == ["late-child", "late-parent"]
        and post_update_child_text == "late child updated"
    )
    frames: list[dict] = []
    broadcaster = SessionWSBroadcaster(object())
    broadcaster._dispatch = lambda payload: frames.append(payload)
    for change in changes:
        broadcaster.on_change(sid, change)
    ok_ws = (
        len(frames) == len(changes)
        and all(frame.get("type") == "messages_delta" for frame in frames)
        and all(
            ((frame.get("data") or {}).get("messages") or [{}])[0].get("id")
            in {"msg-1", "msg-2"}
            for frame in frames
        )
    )
    print(
        f"{PASS if ok_live else FAIL} late resolution updates exact realtime "
        f"message -- changes={len(changes)}",
    )
    print(
        f"{PASS if ok_identity else FAIL} realtime projection equals cold "
        f"restore -- live={post_update} {restored=}",
    )
    print(
        f"{PASS if ok_replacement else FAIL} post-reorder same-uuid update "
        f"replaces correct event -- {post_update=}",
    )
    print(
        f"{PASS if ok_ws else FAIL} late resolution broadcasts exact "
        f"messages_delta -- frames={len(frames)}",
    )
    writer.close()
    return ok_live and ok_identity and ok_replacement and ok_ws


def main() -> int:
    try:
        return 0 if asyncio.run(_run()) else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
