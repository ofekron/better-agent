from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

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
  let session = null;
  let sessionFetch = null;
  if (sessionId) {
    try {
      const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}?msg_limit=200`, {
        credentials: "include",
      });
      sessionFetch = { ok: response.ok, status: response.status };
      if (response.ok) session = await response.json();
    } catch (error) {
      sessionFetch = { ok: false, error: String(error) };
    }
  }
  return {
    url: location.href,
    title: document.title,
    session_id: sessionId,
    tree,
    session,
    sessionFetch,
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
    mismatches = compare_chat_panel_payload(payload)
    return ChatPanelValidation(
        ok=len(mismatches) == 0,
        adapter_id=adapter_id,
        session_id=_string_or_none(payload.get("session_id")) or session_id,
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
        return [f"session snapshot unavailable: {payload.get('sessionFetch')}"]

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
    expected: list[dict[str, str]],
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
) -> list[dict[str, str]] | None:
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
) -> list[dict[str, str]]:
    messages = node.get("messages")
    if not isinstance(messages, list):
        return []
    out: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if include is not None and not include(message):
            continue
        message_id = _string_or_none(message.get("id"))
        role = _string_or_none(message.get("role"))
        if message_id and role in {"user", "assistant"}:
            out.append({"id": message_id, "role": role})
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
    messages: list[dict[str, str]],
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
