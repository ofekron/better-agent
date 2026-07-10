from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from continuation import build_continuation_prompt


@dataclass(frozen=True)
class ContinuationStart:
    prompt: str
    continuation_chain: list[str]
    chain_depth: int


def start_continuation_for(
    *,
    session_manager: Any,
    app_session_id: str,
    prompt: str,
    old_provider_sid: str | None,
    reason: str = "context_exceeded",
    origin: str = "user",
) -> ContinuationStart:
    session = session_manager.get(app_session_id) or {}
    chain = list(session.get("continuation_chain") or [])
    if old_provider_sid:
        chain.append(old_provider_sid)
        session_manager.set_continuation_chain(app_session_id, chain)

    next_prompt = build_continuation_prompt(
        prompt=prompt,
        app_session_id=app_session_id,
        continuation_chain=chain,
        reason=reason,
        origin=origin,
    )
    return ContinuationStart(
        prompt=next_prompt,
        continuation_chain=chain,
        chain_depth=len(chain),
    )
