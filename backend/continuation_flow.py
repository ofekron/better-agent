from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from continuation import build_continuation_prompt


_RECALL_TOOL_PROVIDER_KINDS = {"claude", "claude-remote", "codex"}


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
    provider_kind: str,
    old_provider_sid: str | None,
    reason: str = "context_exceeded",
) -> ContinuationStart:
    session = session_manager.get(app_session_id) or {}
    chain = list(session.get("continuation_chain") or [])
    if old_provider_sid:
        chain.append(old_provider_sid)
        session_manager.set_continuation_chain(app_session_id, chain)

    from session_recall import build_index, recall

    recall_results: list[dict] = []
    try:
        build_index(app_session_id)
        for item in recall(app_session_id, prompt, k=8):
            item["source_session_id"] = app_session_id
            recall_results.append(item)
    except Exception:
        pass

    next_prompt = build_continuation_prompt(
        prompt=prompt,
        app_session_id=app_session_id,
        continuation_chain=chain,
        recall_results=recall_results,
        has_recall_tool=provider_kind in _RECALL_TOOL_PROVIDER_KINDS,
        reason=reason,
    )
    return ContinuationStart(
        prompt=next_prompt,
        continuation_chain=chain,
        chain_depth=len(chain),
    )
