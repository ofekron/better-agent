from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any

from continuation import (
    PROVIDER_CAPABILITIES_CHANGED_ERROR,
    build_continuation_prompt,
)
from render_stub import materialize_message_content


_CAPABILITY_REFERENT_MAX_CHARS = 8_000


@dataclass(frozen=True)
class ContinuationStart:
    prompt: str
    continuation_chain: list[str]
    chain_depth: int


def _bounded_exchange_json(user: str, assistant: str, limit: int) -> str:
    if limit < len(json.dumps({"user": "", "assistant": ""})):
        raise ValueError("capability referent limit is too small")
    values = {"user": user, "assistant": assistant}
    rendered = json.dumps(values, ensure_ascii=False)
    while len(rendered) > limit:
        key = max(values, key=lambda name: len(values[name]))
        value = values[key]
        excess = len(rendered) - limit
        keep = max(0, len(value) - excess - 1)
        values[key] = value[:keep] + ("…" if keep else "")
        rendered = json.dumps(values, ensure_ascii=False)
    return rendered


def _is_iso_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.strip() or "T" not in value:
        return False
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return True


def _capability_restart_prompt(
    session: dict,
    target_assistant_msg_id: str | None,
) -> str:
    if not isinstance(target_assistant_msg_id, str) or not target_assistant_msg_id:
        raise ValueError("capability restart requires a target assistant message")
    messages = session.get("messages")
    if not isinstance(messages, list):
        raise ValueError("capability restart session has no messages")
    matches = [
        index
        for index, message in enumerate(messages)
        if (
            isinstance(message, dict)
            and message.get("role") == "assistant"
            and message.get("id") == target_assistant_msg_id
        )
    ]
    if len(matches) != 1 or matches[0] < 3:
        raise ValueError("capability restart target has no immediate prior exchange")

    target_index = matches[0]
    prior_user, prior_assistant, current_user = messages[target_index - 3:target_index]
    if not all(isinstance(message, dict) for message in (
        prior_user, prior_assistant, current_user,
    )):
        raise ValueError("capability restart messages are malformed")
    if any(message.get("source") for message in (
        prior_user, prior_assistant, current_user,
    )):
        raise ValueError("capability restart target crosses a subturn boundary")
    if prior_user.get("role") != "user" or current_user.get("role") != "user":
        raise ValueError("capability restart target is not adjacent to user messages")
    if (
        prior_assistant.get("role") != "assistant"
        or not _is_iso_timestamp(prior_assistant.get("completed_at"))
        or prior_assistant.get("error") is not None
        or prior_assistant.get("stopped_at") is not None
    ):
        raise ValueError("capability restart prior assistant is incomplete")

    prior_user_content = prior_user.get("content")
    current_user_content = current_user.get("content")
    if (
        not isinstance(prior_user_content, str)
        or not prior_user_content.strip()
        or not isinstance(current_user_content, str)
        or not current_user_content.strip()
    ):
        raise ValueError("capability restart user content is missing")
    materialized_assistant = materialize_message_content(prior_assistant)
    if not isinstance(materialized_assistant, str) or not materialized_assistant.strip():
        raise ValueError("capability restart assistant content is missing")
    prior_assistant_content = materialized_assistant.strip()

    header = (
        "Authoritative immediate conversation before the provider restart. "
        "Use it to resolve pronouns and referents in the current user message; "
        "restart metadata is not the user's topic unless they ask about it.\n"
    )
    exchange = _bounded_exchange_json(
        prior_user_content,
        prior_assistant_content,
        _CAPABILITY_REFERENT_MAX_CHARS - len(header),
    )
    return (
        header
        + exchange
        + "\n\nCurrent user message:\n"
        + current_user_content
    )


def start_continuation_for(
    *,
    session_manager: Any,
    app_session_id: str,
    prompt: str,
    old_provider_sid: str | None,
    reason: str = "context_exceeded",
    target_assistant_msg_id: str | None = None,
) -> ContinuationStart:
    session = session_manager.get(app_session_id) or {}
    continuation_prompt = prompt
    if reason == PROVIDER_CAPABILITIES_CHANGED_ERROR:
        continuation_prompt = _capability_restart_prompt(
            session,
            target_assistant_msg_id,
        )

    chain = list(session.get("continuation_chain") or [])
    if old_provider_sid:
        chain.append(old_provider_sid)

    next_prompt = build_continuation_prompt(
        prompt=continuation_prompt,
        app_session_id=app_session_id,
        continuation_chain=chain,
        reason=reason,
    )
    if old_provider_sid:
        session_manager.set_continuation_chain(app_session_id, chain)
    return ContinuationStart(
        prompt=next_prompt,
        continuation_chain=chain,
        chain_depth=len(chain),
    )
