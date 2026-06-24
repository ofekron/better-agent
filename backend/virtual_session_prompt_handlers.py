from __future__ import annotations

from typing import Any, Awaitable, Callable

DispatchWS = Callable[[dict[str, Any]], Awaitable[None]]
PromptHandler = Callable[
    [str, str, str, str | None, str | None, DispatchWS],
    Awaitable[bool],
]

_handlers: dict[str, PromptHandler] = {}


def register(session_id: str, handler: PromptHandler) -> None:
    if not session_id:
        raise ValueError("session_id is required")
    _handlers[session_id] = handler


async def handle(
    session_id: str,
    *,
    prompt: str,
    cwd: str,
    client_id: str | None,
    lifecycle_msg_id: str | None,
    dispatch_ws: DispatchWS,
) -> bool:
    handler = _handlers.get(session_id)
    if handler is None:
        return False
    return await handler(session_id, prompt, cwd, client_id, lifecycle_msg_id, dispatch_ws)
