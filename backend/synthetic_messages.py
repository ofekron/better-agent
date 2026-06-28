from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

from session_manager import manager as session_manager
from user_msg_lifecycle import emit_queued, new_lifecycle_msg_id


async def inject(
    coordinator: Any,
    session_id: str,
    *,
    prompt: str,
    model: str = "",
    cwd: str = "",
    orchestration_mode: str = "",
    client_id: str = "",
    source: str = "synthetic",
    display_prompt: str = "",
    capability_contexts: list[dict] | None = None,
) -> dict:
    if not session_id:
        raise ValueError("session_id is required")
    if not prompt.strip():
        raise ValueError("prompt is required")
    session = await asyncio.to_thread(session_manager.get_lite, session_id)
    if not session:
        raise ValueError("session not found")

    lifecycle_msg_id = new_lifecycle_msg_id()
    item_id = str(uuid.uuid4())
    is_queued = (
        coordinator.turn_manager.has_active_turn(session_id)
        or coordinator.turn_manager.has_active_runs(session_id)
    )
    lifecycle_kind = "queued_behind" if is_queued else "send"
    queue_position = coordinator.get_queued_count(session_id)
    effective_prompt = display_prompt or prompt
    effective_mode = orchestration_mode or session.get("orchestration_mode")

    params = {
        "prompt": effective_prompt,
        "app_session_id": session_id,
        "model": model or session.get("model"),
        "cwd": cwd if cwd else session.get("cwd"),
        "ws_callback": None,
        "orchestration_mode": effective_mode,
        "client_id": client_id or None,
        "lifecycle_msg_id": lifecycle_msg_id,
        "cli_prompt": prompt,
        "source": source,
        "capability_contexts": capability_contexts,
        "_queued_id": item_id,
    }
    await asyncio.to_thread(
        session_manager.add_queued_prompt,
        session_id,
        {
            "id": item_id,
            "lifecycle_msg_id": lifecycle_msg_id,
            "content": effective_prompt,
            "kind": lifecycle_kind,
            "queue_position": queue_position,
            "images_count": 0,
            "files_count": 0,
            "orchestration_mode": effective_mode,
            "cli_prompt": prompt,
            "client_id": client_id or None,
            "source": source,
            "capability_contexts": capability_contexts,
            "created_at": datetime.now().isoformat(),
        },
    )
    try:
        await coordinator.submit_prompt_async(session_id, params)
    except Exception:
        await asyncio.to_thread(session_manager.remove_queued_prompt, session_id, item_id)
        raise

    await emit_queued(
        app_session_id=session_id,
        lifecycle_msg_id=lifecycle_msg_id,
        content=effective_prompt,
        kind=lifecycle_kind,
        queue_position=queue_position,
        client_id=client_id or None,
        images_count=0,
        orchestration_mode=effective_mode,
    )
    return {
        "success": True,
        "session_id": session_id,
        "queued_id": item_id,
        "lifecycle_msg_id": lifecycle_msg_id,
        "queued": is_queued,
    }


async def append_assistant_message(
    session_id: str,
    *,
    content: str,
    source: str = "synthetic",
) -> dict:
    if not session_id:
        raise ValueError("session_id is required")
    if not content.strip():
        raise ValueError("content is required")
    session = await asyncio.to_thread(session_manager.get_lite, session_id)
    if not session:
        raise ValueError("session not found")

    now = datetime.now().isoformat()
    event_uuid = str(uuid.uuid4())
    msg = {
        "id": str(uuid.uuid4()),
        "role": "assistant",
        "content": content,
        "events": [
            {
                "type": "agent_message",
                "data": {
                    "type": "assistant",
                    "uuid": event_uuid,
                    "timestamp": now,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": content}],
                    },
                },
            }
        ],
        "timestamp": now,
        "isStreaming": False,
        "completed_at": now,
        "source": source,
    }
    result = await asyncio.to_thread(
        session_manager.append_assistant_msg,
        session_id,
        msg,
    )
    if result is None:
        raise ValueError("session not found")
    from event_journal import publish_event
    await publish_event(
        session_id=session_id,
        context_id=session_id,
        event_type="agent_message",
        data=msg["events"][0]["data"],
        source=source,
        message_id=msg["id"],
        event_id=event_uuid,
    )
    return {"success": True, "session_id": session_id, "message": msg}
