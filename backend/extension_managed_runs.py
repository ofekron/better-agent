from __future__ import annotations

import asyncio
import re
from typing import Any

from orchs._subprocess_agent import SubprocessAgent
from session_manager import manager as session_manager

_EVENT_PREFIX_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def _text_from_events(events: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        data = event.get("data") or {}
        if event.get("type") != "agent_message" or data.get("type") != "assistant":
            continue
        message = data.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str) and content:
            parts.append(content)
            continue
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        parts.append(text)
    return " ".join(parts).strip()


async def run(
    coordinator,
    *,
    managed_session_id: str,
    parent_session_id: str,
    prompt: str,
    model: str,
    cwd: str,
    init_prompt: str = "",
    agent_sid: str = "",
    event_prefix: str = "managed_run",
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not _EVENT_PREFIX_RE.fullmatch(event_prefix):
        raise ValueError("event_prefix must match ^[a-z][a-z0-9_]{0,39}$")
    if not prompt:
        raise ValueError("prompt is required")
    managed = session_manager.get_lite(managed_session_id)
    if not managed:
        raise ValueError("managed_session_id does not exist")
    if parent_session_id and not session_manager.get_lite(parent_session_id):
        raise ValueError("parent_session_id does not exist")

    agent = SubprocessAgent(
        agent_session_id=managed_session_id,
        cwd=cwd or str(managed.get("cwd") or ""),
        extra_env=extra_env or None,
    )
    if agent_sid:
        agent.agent_sid = agent_sid
        agent.initialized = True

    callbacks = coordinator.ws_callbacks.get(parent_session_id) if parent_session_id else None

    async def ws_callback(event: dict) -> None:
        if not callbacks:
            return
        for callback in callbacks:
            try:
                await callback(event)
            except Exception:
                pass

    cancel_event = (
        coordinator.turn_manager.cancel_events.get(parent_session_id)
        or coordinator.turn_manager.cancel_events.get(managed_session_id)
        or asyncio.Event()
    )
    run_model = model or str(managed.get("model") or "")

    if not agent.initialized and init_prompt:
        discovered = await agent.init(
            coordinator,
            model=run_model,
            prep_prompt=init_prompt,
            cancel_event=cancel_event,
            ws_callback=ws_callback,
            mode="native",
            ws_event_prefix=event_prefix,
        )
        if not discovered:
            return {
                "success": False,
                "error": "init_failed",
                "agent_sid": agent.agent_sid,
                "events": [],
                "text": "",
            }

    result = await agent.run_turn(
        coordinator,
        prompt=prompt,
        model=run_model,
        ws_callback=ws_callback,
        cancel_event=cancel_event,
        mode="native",
        session_id=agent.agent_sid or None,
    )
    events = result.get("events") if isinstance(result, dict) else []
    if not isinstance(events, list):
        events = []
    next_agent_sid = agent.agent_sid
    if isinstance(result, dict):
        next_agent_sid = agent.agent_sid or str(result.get("session_id") or "")
    return {
        **(result if isinstance(result, dict) else {"success": False, "error": "run_failed"}),
        "agent_sid": next_agent_sid,
        "text": _text_from_events(events),
    }
