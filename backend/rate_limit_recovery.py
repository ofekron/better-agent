from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

from event_bus import bus
from session_manager import manager as session_manager


class RateLimitRecoveryError(RuntimeError):
    pass


async def continue_and_wait(
    session_id: str,
    assistant_message_id: str,
    start: Callable[[], dict[str, Any]],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    terminal = loop.create_future()
    subscriber_name = f"rate-limit-recovery-{uuid.uuid4().hex}"

    async def on_terminal(event) -> None:
        if event.sid == session_id and not terminal.done():
            terminal.set_result(event)

    bus.subscribe(
        "lifecycle.turn_complete",
        on_terminal,
        name=subscriber_name,
    )
    try:
        accepted = start()
        await asyncio.wait_for(terminal, timeout=timeout_seconds)
        session = await asyncio.to_thread(session_manager.get, session_id)
        messages = list((session or {}).get("messages") or [])
        failed_index = next((
            index
            for index, message in enumerate(messages)
            if message.get("id") == assistant_message_id
        ), -1)
        completed = [
            message
            for message in messages[failed_index + 1:]
            if message.get("role") == "assistant"
            and message.get("completed_at")
            and not message.get("retrying_until")
        ]
        if failed_index < 0 or not completed:
            raise RateLimitRecoveryError(
                "continued turn completed without a persisted assistant result"
            )
        assistant = completed[-1]
        if assistant.get("error") or assistant.get("errorText"):
            raise RateLimitRecoveryError(
                str(assistant.get("errorText") or "continued turn failed")
            )
        return {
            **accepted,
            "completed": True,
            "assistant_message_id": assistant.get("id"),
            "assistant_content": assistant.get("content"),
            "provider_id": (session or {}).get("provider_id"),
            "model": (session or {}).get("model"),
            "run_meta": assistant.get("run_meta"),
        }
    finally:
        bus.unsubscribe(subscriber_name)
