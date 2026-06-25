from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from session_manager import manager as session_manager
from testape_login_detector import (
    FS_DEFAULT,
    _assert_loopback,
    _coerce_markers,
    _web_session,
    list_web_adapters,
)

_CHAT_PANEL_JS_TEMPLATE = """
(async () => {
  const requestedSessionId = __SESSION_ID__;
  const api = window.__betterAgentTestApe;
  const tree = api && typeof api.extractVisibleChatPanelTree === "function"
    ? api.extractVisibleChatPanelTree()
    : null;
  const pathMatch = location.pathname.match(/^\\/s\\/([^/]+)(?:\\/.*)?$/);
  const pathSessionId = pathMatch ? decodeURIComponent(pathMatch[1]) : null;
  const sessionId = requestedSessionId || (tree && tree.session_id) || pathSessionId;
  return {
    url: location.href,
    title: document.title,
    session_id: sessionId,
    tree,
  };
})()
"""


@dataclass
class ChatPanelValidation:
    ok: bool
    adapter_id: str
    session_id: str | None
    url: str | None = None
    title: str | None = None
    tree: dict[str, Any] | None = None
    mismatches: list[str] | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_chat_panel(
    adapter_id: str | None = None,
    session_id: str | None = None,
    url: str | None = None,
    fs_url: str = FS_DEFAULT,
) -> ChatPanelValidation:
    if url:
        _assert_loopback(url)

    if not adapter_id:
        adapters = list_web_adapters(fs_url)
        if not adapters:
            return ChatPanelValidation(
                False,
                "",
                session_id,
                reason="no connected TestApe web adapter",
            )
        last_error: Exception | None = None
        for candidate_id, _name in adapters:
            try:
                return _validate_for_adapter(candidate_id, session_id, url, fs_url)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        reason = "no usable connected TestApe web adapter"
        if last_error is not None:
            reason = f"{reason}: {last_error}"
        return ChatPanelValidation(False, "", session_id, reason=reason)

    return _validate_for_adapter(adapter_id, session_id, url, fs_url)


def _validate_for_adapter(
    adapter_id: str,
    session_id: str | None,
    url: str | None,
    fs_url: str,
) -> ChatPanelValidation:
    script = _CHAT_PANEL_JS_TEMPLATE.replace("__SESSION_ID__", json.dumps(session_id))
    with _web_session(adapter_id, fs_url) as web:
        if url:
            web.navigate(url)
        raw = web.eval_js(script)

    payload = _coerce_markers(raw)
    detected_session_id = _string_or_none(payload.get("session_id")) or session_id
    if detected_session_id:
        payload["session"] = session_manager.get_root_tree(detected_session_id)
    mismatches = compare_chat_panel_payload(payload)
    return ChatPanelValidation(
        ok=len(mismatches) == 0,
        adapter_id=adapter_id,
        session_id=detected_session_id,
        url=_string_or_none(payload.get("url")),
        title=_string_or_none(payload.get("title")),
        tree=payload.get("tree") if isinstance(payload.get("tree"), dict) else None,
        mismatches=mismatches,
        reason=None if len(mismatches) == 0 else "visible chat panel does not match session data",
    )


def compare_chat_panel_payload(payload: dict[str, Any]) -> list[str]:
    tree = payload.get("tree")
    session = payload.get("session")
    if not isinstance(tree, dict):
        return ["Better Agent TestApe chat panel extractor is not installed"]
    if tree.get("visible") is not True:
        return ["chat panel is not visible"]
    if not isinstance(session, dict):
        return ["session snapshot unavailable"]

    session_id = _string_or_none(payload.get("session_id")) or _string_or_none(tree.get("session_id"))
    nodes = _nodes_by_id(session)
    root = nodes.get(_string_or_none(session.get("id")) or "")
    if root is None:
        return ["session snapshot has no root node"]

    regions = tree.get("regions")
    if not isinstance(regions, list) or len(regions) == 0:
        return ["chat panel has no rendered regions"]

    mismatches: list[str] = []
    for index, region in enumerate(regions):
        if not isinstance(region, dict):
            mismatches.append(f"region {index} is not an object")
            continue
        expected = _expected_messages_for_region(region, session_id, root, nodes)
        if expected is None:
            mismatches.append(
                f"region {index} has no matching session node for {region.get('session_id')!r}"
            )
            continue
        mismatches.extend(_compare_region(index, region, expected))
    return mismatches


def _compare_region(
    index: int,
    region: dict[str, Any],
    expected: list[dict[str, str | None]],
) -> list[str]:
    rendered = region.get("messages")
    if not isinstance(rendered, list):
        return [f"region {index} messages is not a list"]
    expected_by_id = {message["id"]: message for message in expected}
    out: list[str] = []
    cursor = 0
    for rendered_index, item in enumerate(rendered):
        if not isinstance(item, dict):
            out.append(f"region {index} message {rendered_index} is not an object")
            continue
        message_id = _string_or_none(item.get("id"))
        role = _string_or_none(item.get("role"))
        if not message_id:
            out.append(f"region {index} message {rendered_index} has no id")
            continue
        expected_message = expected_by_id.get(message_id)
        if expected_message is None:
            out.append(f"region {index} rendered unexpected message {message_id}")
            continue
        if role != expected_message["role"]:
            out.append(
                f"region {index} message {message_id} role {role!r} != {expected_message['role']!r}"
            )
        rendered_text = _normalize_text(_string_or_none(item.get("text")) or "")
        expected_texts = expected_message.get("texts") or []
        if expected_texts and not any(text in rendered_text for text in expected_texts):
            out.append(f"region {index} message {message_id} text does not contain expected content")
        next_cursor = _find_message_index(expected, message_id, cursor)
        if next_cursor is None:
            out.append(f"region {index} message {message_id} is out of session order")
        else:
            cursor = next_cursor + 1
    return out


def _expected_messages_for_region(
    region: dict[str, Any],
    current_session_id: str | None,
    root: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
) -> list[dict[str, str | None]] | None:
    kind = _string_or_none(region.get("kind")) or "linear"
    if kind == "fork_shared":
        fork_point = _earliest_fork_point(root)
        return _message_entries(
            root,
            lambda message: fork_point is None or _seq(message) is None or _seq(message) <= fork_point,
        )
    session_id = _string_or_none(region.get("session_id")) or current_session_id or _string_or_none(root.get("id"))
    node = nodes.get(session_id or "")
    if node is None:
        return None
    if kind == "fork_pane":
        fork_point = _earliest_fork_point(root)
        return _message_entries(
            node,
            lambda message: fork_point is None or (_seq(message) is not None and _seq(message) > fork_point),
        )
    return _message_entries(node)


def _nodes_by_id(root: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}

    def visit(node: dict[str, Any]) -> None:
        node_id = _string_or_none(node.get("id"))
        if node_id and (node.get("kind") or "user") == "user":
            out[node_id] = node
        for child in node.get("forks") or []:
            if isinstance(child, dict):
                visit(child)

    visit(root)
    return out


def _message_entries(
    node: dict[str, Any],
    include: Any | None = None,
) -> list[dict[str, str | None]]:
    messages = node.get("messages")
    if not isinstance(messages, list):
        return []
    out: list[dict[str, str | None]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if include is not None and not include(message):
            continue
        message_id = _string_or_none(message.get("id"))
        role = _string_or_none(message.get("role"))
        if message_id and role in {"user", "assistant"}:
            out.append({"id": message_id, "role": role, "texts": _message_text_candidates(message)})
    return out


def _earliest_fork_point(root: dict[str, Any]) -> int | None:
    earliest: int | None = None

    def visit(node: dict[str, Any]) -> None:
        nonlocal earliest
        if (node.get("kind") or "user") != "user":
            return
        fork_point = node.get("fork_point_seq")
        if isinstance(fork_point, int) and (earliest is None or fork_point < earliest):
            earliest = fork_point
        for child in node.get("forks") or []:
            if isinstance(child, dict):
                visit(child)

    visit(root)
    return earliest


def _find_message_index(
    messages: list[dict[str, str | None]],
    message_id: str,
    start: int,
) -> int | None:
    for index in range(start, len(messages)):
        if messages[index]["id"] == message_id:
            return index
    return None


def _seq(message: dict[str, Any]) -> int | None:
    value = message.get("seq")
    return value if isinstance(value, int) else None


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _message_text_candidates(message: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    content = message.get("content")
    if isinstance(content, str):
        texts.extend(_display_text_candidates(content))
    elif isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
        texts.extend(_display_text_candidates(" ".join(parts)))
    for event in _message_events(message):
        texts.extend(_display_text_candidates(_assistant_event_text(event)))
    return [text for index, text in enumerate(texts) if text and text not in texts[:index]]


def _message_events(message: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    events = message.get("events")
    if isinstance(events, list):
        out.extend(event for event in events if isinstance(event, dict))
    manager = message.get("manager")
    if isinstance(manager, dict):
        manager_events = manager.get("events")
        if isinstance(manager_events, list):
            out.extend(event for event in manager_events if isinstance(event, dict))
    workers = message.get("workers")
    if isinstance(workers, list):
        for worker in workers:
            if not isinstance(worker, dict):
                continue
            worker_events = worker.get("events")
            if isinstance(worker_events, list):
                out.extend(event for event in worker_events if isinstance(event, dict))
    return out


def _assistant_event_text(event: dict[str, Any]) -> str:
    data = event.get("data")
    if isinstance(data, dict) and data.get("type") == "assistant":
        message = data.get("message")
        if isinstance(message, dict):
            return _content_parts_text(message.get("content"))
    if event.get("type") == "agent_message" and isinstance(data, dict):
        inner = data.get("message")
        if isinstance(inner, dict):
            return _content_parts_text(inner.get("content"))
    return ""


def _content_parts_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str):
            parts.append(text)
    return " ".join(parts)


def _display_text_candidates(value: str) -> list[str]:
    normalized = _normalize_text(value)
    stripped = _normalize_text(_strip_markdown(value))
    return [candidate for candidate in (normalized, stripped) if candidate]


def _strip_markdown(value: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", value)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_~>#-]+", " ", text)
    return text


def _normalize_text(value: str) -> str:
    return " ".join(value.split())
