from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from codex_normalize import (
    _attach_collab_parent_from_thread,
    _codex_agent_message_parts,
    _codex_terminal_state,
    _normalize_agent_message,
    _normalize_collab_agent_completed,
    _normalize_collab_agent_started,
    _normalize_command_started,
    _normalize_error_item,
    _normalize_event_msg_notice,
    _normalize_event_msg_patch_apply_end,
    _normalize_event_msg_reasoning,
    _normalize_event_msg_text,
    _normalize_file_change,
    _normalize_mcp_tool_completed,
    _normalize_mcp_tool_started,
    _normalize_native_payload,
    _normalize_reasoning,
    _normalize_response_item_event,
    _normalize_response_tool_call,
    _normalize_response_tool_result,
    _normalize_sub_agent_activity,
    _normalize_todo_list,
    _normalize_web_search,
    _normalize_web_search_events,
    _new_uuid,
    _remember_collab_receivers,
    _response_item_uuid,
    _stable_uuid,
    _web_search_dedupe_key,
    _web_search_item_from_payload,
)

logger = logging.getLogger(__name__)

_CODEX_NON_RENDERABLE_EVENT_MSG_TYPES = {
    "thread_settings_applied",
    "world_state",
}

_CODEX_NON_RENDERABLE_TOP_LEVEL_TYPES = {
    "thread_settings_applied",
    "world_state",
}


def codex_state_db_path() -> Path:
    return Path.home() / ".codex" / "state_5.sqlite"


def codex_state_db_paths() -> list[Path]:
    return [
        codex_state_db_path(),
        codex_legacy_state_db_path(),
    ]


def _resolve_rollout_path_from_db(db_path: Path, thread_id: str) -> Optional[Path]:
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "select rollout_path from threads where id = ?",
                (thread_id,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.exception(
            "failed querying codex state db %s for thread %s", db_path, thread_id
        )
        return None
    if not row or not row[0]:
        return None
    path = Path(str(row[0]))
    return path if path.exists() else None


def codex_legacy_state_db_path() -> Path:
    return Path.home() / ".codex" / "sqlite" / "state_5.sqlite"


def resolve_rollout_path(thread_id: str) -> Optional[Path]:
    if not thread_id:
        return None
    for db_path in codex_state_db_paths():
        path = _resolve_rollout_path_from_db(db_path, thread_id)
        if path is not None:
            return path
    return None


def _append_unique_subagent_id(ids: list[str], value: Any) -> None:
    if isinstance(value, str) and value and value not in ids:
        ids.append(value)


def _json_value(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _append_subagent_ids_from_spawn_payload(ids: list[str], payload: Any) -> None:
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, str):
                _append_unique_subagent_id(ids, item)
            elif isinstance(item, dict):
                _append_unique_subagent_id(ids, item.get("agent_id"))
                _append_unique_subagent_id(ids, item.get("id"))
        return
    if not isinstance(payload, dict):
        return
    _append_unique_subagent_id(ids, payload.get("agent_id"))
    agent_ids = payload.get("agent_ids")
    if isinstance(agent_ids, list):
        for agent_id in agent_ids:
            _append_unique_subagent_id(ids, agent_id)
    agents = payload.get("agents")
    if isinstance(agents, list):
        for agent in agents:
            if isinstance(agent, str):
                _append_unique_subagent_id(ids, agent)
            elif isinstance(agent, dict):
                _append_unique_subagent_id(ids, agent.get("agent_id"))
                _append_unique_subagent_id(ids, agent.get("id"))


def codex_subagent_ids_from_event(event: dict) -> list[str]:
    return [
        source["child_id"]
        for source in codex_subagent_sources_from_event(event)
    ]


def codex_subagent_sources_from_event(event: dict) -> list[dict[str, str]]:
    ids: list[str] = []
    _append_unique_subagent_id(ids, event.get("codex_subagent_id"))
    message = event.get("message")
    if not isinstance(message, dict):
        return _codex_subagent_sources(event, ids)
    content = message.get("content")
    if not isinstance(content, list) or not content:
        return _codex_subagent_sources(event, ids)
    block = content[0]
    if not isinstance(block, dict) or block.get("type") != "tool_result":
        return _codex_subagent_sources(event, ids)
    raw = block.get("content")
    payload = _json_value(raw)
    if event.get("codex_spawn_agent_result"):
        _append_subagent_ids_from_spawn_payload(ids, payload)
        return _codex_subagent_sources(event, ids)
    if event.get("codex_wait_agent_result") and isinstance(payload, dict):
        status = payload.get("status")
        if isinstance(status, dict):
            for key in status:
                _append_unique_subagent_id(ids, key)
    return _codex_subagent_sources(event, ids)


def _codex_subagent_sources(event: dict, ids: list[str]) -> list[dict[str, str]]:
    parent_tool_use_id = ""
    message = event.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            value = content[0].get("tool_use_id")
            if isinstance(value, str):
                parent_tool_use_id = value
    value = event.get("parent_tool_use_id")
    if isinstance(value, str) and value:
        parent_tool_use_id = value
    sources: list[dict[str, str]] = []
    for child_id in ids:
        delegation_id = codex_subagent_delegation_id(
            child_id,
            parent_tool_use_id=parent_tool_use_id,
        )
        source_key = delegation_id[len("codex_subagent_"):]
        sources.append({
            "source_key": source_key,
            "child_id": child_id,
            "parent_tool_use_id": parent_tool_use_id,
            "delegation_id": delegation_id,
        })
    return sources


def codex_subagent_delegation_id(
    child_id: str,
    *,
    parent_tool_use_id: str = "",
) -> str:
    if parent_tool_use_id:
        return f"codex_subagent_{parent_tool_use_id}_{child_id}"
    return f"codex_subagent_{child_id}"


def codex_subagent_id_from_event(event: dict) -> Optional[str]:
    ids = codex_subagent_ids_from_event(event)
    return ids[0] if ids else None


def codex_subagent_rollout_start_byte(path: Path) -> int:
    try:
        with path.open("rb") as f:
            while True:
                raw = f.readline()
                if not raw:
                    return 0
                try:
                    row = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                payload = row.get("payload")
                if (
                    row.get("type") == "event_msg"
                    and isinstance(payload, dict)
                    and payload.get("type") == "user_message"
                ):
                    return f.tell()
    except OSError:
        return 0


async def resolve_rollout_path_polled(
    thread_id: str, *, timeout: float = 15.0, poll_interval: float = 0.25
) -> Optional[Path]:
    """Bounded retry around resolve_rollout_path.

    Codex's app-server emits thread.started — and the runner therefore writes
    state.json — before the thread row is committed to the codex sqlite DB, so
    a single resolve at that instant returns None (a false "missing rollout
    path"). The row always lands shortly after, so poll until it shows up or
    the deadline passes; only then is the run genuinely unresolvable.
    """
    import time

    deadline = time.monotonic() + timeout
    while True:
        path = resolve_rollout_path(thread_id)
        if path is not None:
            return path
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(poll_interval)


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
            continue
        text = block.get("input_text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(part for part in parts if part)


def _compact_replacement_history(history: Any) -> list[dict]:
    if not isinstance(history, list):
        return []
    rows: list[dict] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        text = _content_text(item.get("content"))
        if not isinstance(role, str) or not text:
            continue
        rows.append({"role": role, "text": text})
    return rows


class CodexRolloutNormalizer:
    def __init__(self, *, namespace: str) -> None:
        self.namespace = namespace or "codex"
        self.parent_uuid = _stable_uuid(self.namespace, "root")
        self.started_items: dict[str, str] = {}
        self.response_tool_parents: dict[str, str] = {}
        self.response_tool_names: dict[str, str] = {}
        self.response_agent_tool_parents: dict[str, str] = {}
        self.response_agent_ids: dict[str, list[tuple[str, str]]] = {}
        self.emitted_tool_result_ids: set[str] = set()
        self.collab_thread_parents: dict[str, str] = {}
        self.seen_web_search_calls: set[str] = set()
        self.seen_web_search_keys: set[str] = set()
        # USER STAMPED: every Codex rollout line carries a top-level
        # `timestamp` (when Codex produced the event). The shared
        # `_normalize_*` helpers don't receive it and stamp `datetime.now()`,
        # discarding the real time. The normalizer re-stamps every event
        # it emits with the source line's timestamp at this single chokepoint.
        self._line_timestamp: Optional[str] = None
        # Assistant text emitted this turn, keyed by exact text. Codex streams
        # every assistant utterance via `event_msg.agent_message` (including
        # intermediate commentary) and re-emits only the finalized answer as a
        # `response_item.message` (role=assistant). First-wins dedup keeps each
        # utterance exactly once across the two sources, so the finalized echo
        # doesn't double up with the streamed copy. Scoped per turn so identical
        # short text in different turns ("Compact task completed") survives.
        self._seen_turn_assistant_texts: set[str] = set()
        self._pending_context_compacted_uuid: Optional[str] = None
        # Last seen model context window/fill, captured from event_msg.token_count.
        # Not rendered as a card; surfaced to the caller so the existing
        # context-window UI/preemption channel gets it via the complete envelope.
        self.context_window: Optional[int] = None
        self.context_tokens: Optional[int] = None

    def normalize_line(self, raw_line: str) -> list[dict]:
        try:
            raw_event = json.loads(raw_line)
        except json.JSONDecodeError:
            return []
        if not isinstance(raw_event, dict):
            return []
        return self.normalize_event(raw_event)

    def _assistant_text(self, event: Optional[dict]) -> str:
        if not isinstance(event, dict) or event.get("type") != "assistant":
            return ""
        content = (event.get("message") or {}).get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            block = content[0]
            if block.get("type") == "text":
                return block.get("text") or ""
        return ""

    def _claim_assistant_text(self, text: str) -> bool:
        if not text or text in self._seen_turn_assistant_texts:
            return False
        self._seen_turn_assistant_texts.add(text)
        return True

    def _push(self, event: Optional[dict]) -> list[dict]:
        if event is None:
            return []
        new_uuid = event.get("uuid")
        if new_uuid:
            self.parent_uuid = new_uuid
        if self._line_timestamp:
            event["timestamp"] = self._line_timestamp
        return [event]

    def _push_from_native(
        self,
        raw_event: dict,
        event: Optional[dict],
        *,
        uuid_override: Optional[str] = None,
        uuid_suffix: str = "",
    ) -> list[dict]:
        if event is None:
            return []
        try:
            identity = json.dumps(raw_event, sort_keys=True, ensure_ascii=False, default=str)
        except TypeError:
            identity = str(raw_event)
        event["uuid"] = uuid_override or _stable_uuid(self.namespace, f"native:{identity}{uuid_suffix}")
        return self._push(event)

    def normalize_event(self, raw_event: dict) -> list[dict]:
        event_type = raw_event.get("type")
        self._line_timestamp = raw_event.get("timestamp")

        if event_type == "compacted":
            self._seen_turn_assistant_texts.clear()
            rows = self._normalize_compacted_event(
                raw_event,
                uuid_override=self._pending_context_compacted_uuid,
            )
            self._pending_context_compacted_uuid = None
            return rows
        if event_type == "session_meta":
            self._seen_turn_assistant_texts.clear()
            return []
        if event_type == "turn_context":
            self._seen_turn_assistant_texts.clear()
            # Turn context (turn id, cwd, date, timezone, model, effort,
            # approval policy, sandbox) is operational metadata, not rendered
            # chat context.
            return []
        if event_type in ("task_started", "turn.started", "turn.completed", "turn.failed"):
            return []
        if event_type == "thread.started":
            return []
        if event_type == "inter_agent_communication_metadata":
            payload = raw_event.get("payload")
            if isinstance(payload, dict) and set(payload) <= {"trigger_turn"}:
                return []
        if event_type in _CODEX_NON_RENDERABLE_TOP_LEVEL_TYPES:
            return []
        if event_type == "error":
            message = raw_event.get("message")
            if not message:
                return []
            return self._push(_normalize_error_item({"message": message}, self.parent_uuid))

        if event_type == "response_item":
            payload = raw_event.get("payload") or {}
            if not isinstance(payload, dict):
                return []
            return self._normalize_response_payload(payload)

        if event_type == "event_msg":
            payload = raw_event.get("payload") or {}
            if not isinstance(payload, dict):
                return []
            payload_type = payload.get("type")
            if payload_type in _CODEX_NON_RENDERABLE_EVENT_MSG_TYPES:
                return []
            if payload_type == "sub_agent_activity":
                return self._push_from_native(
                    raw_event,
                    _normalize_sub_agent_activity(payload, self.parent_uuid),
                )
            if payload_type == "user_message":
                # User prompts are owned by Better Agent's own scaffolds and
                # never rendered from the provider stream.
                return []
            if payload_type == "agent_message":
                text = payload.get("message")
                if not isinstance(text, str) or not text:
                    return []
                if not self._claim_assistant_text(text):
                    return []
                return self._push_from_native(
                    raw_event,
                    _normalize_event_msg_text(payload, self.parent_uuid, text),
                )
            if payload_type == "token_count":
                # Not a chat card. total_token_usage is cumulative usage across
                # the thread; last_token_usage is the active context occupancy.
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                window = info.get("model_context_window")
                if isinstance(window, int):
                    self.context_window = window
                last_usage = info.get("last_token_usage")
                if isinstance(last_usage, dict):
                    tokens = last_usage.get("total_tokens")
                    if isinstance(tokens, int):
                        self.context_tokens = tokens
                return []
            if payload_type == "task_started":
                # Operational metadata (turn id, context window, collaboration
                # mode) — not rendered chat context, like the turn_context
                # sandbox policy above.
                return []
            if payload_type == "task_complete":
                # Turn-end usage already lands in complete.json via turn.completed.
                return []
            if payload_type in ("context_compacted", "turn_aborted"):
                rows = self._push_from_native(
                    raw_event,
                    _normalize_event_msg_notice(payload, self.parent_uuid)
                )
                if payload_type == "context_compacted" and rows:
                    uuid = rows[0].get("uuid")
                    self._pending_context_compacted_uuid = uuid if isinstance(uuid, str) else None
                return rows
            if payload_type == "patch_apply_end":
                return self._push_from_native(
                    raw_event,
                    _normalize_event_msg_patch_apply_end(payload, self.parent_uuid)
                )
            if payload_type in ("agent_reasoning", "agent_reasoning_delta"):
                message = payload.get("message")
                if isinstance(message, str) and message:
                    return self._push_from_native(
                        raw_event,
                        _normalize_event_msg_reasoning(payload, self.parent_uuid, message)
                    )
            if payload.get("type") == "web_search_end":
                return self._normalize_web_search_payload(payload)
            if payload_type == "mcp_tool_call_end":
                return self._normalize_mcp_tool_call_end(raw_event, payload)
            return self._push_from_native(
                raw_event,
                _normalize_native_payload("event_msg", payload, self.parent_uuid),
            )

        if event_type in ("item.started", "item.updated", "item.completed"):
            item = raw_event.get("item") or {}
            if not isinstance(item, dict):
                return []
            return self._normalize_item_event(str(event_type), item)

        return self._push(_normalize_native_payload(str(event_type or "unknown"), raw_event, self.parent_uuid))

    def _normalize_compacted_event(
        self,
        raw_event: dict,
        *,
        uuid_override: Optional[str] = None,
    ) -> list[dict]:
        payload = raw_event.get("payload")
        if not isinstance(payload, dict):
            return []
        replacement = _compact_replacement_history(payload.get("replacement_history"))
        data = {
            "kind": "compacted",
            "message": "Context compacted",
            "timestamp": datetime.now().isoformat(),
        }
        if replacement:
            data["replacement_history"] = replacement
        return self._push_from_native(
            raw_event,
            {
                "type": "lifecycle_notice",
                "data": data,
                "uuid": _new_uuid(),
                "parentUuid": self.parent_uuid,
            },
            uuid_override=uuid_override,
        )

    def _normalize_mcp_tool_call_end(self, raw_event: dict, payload: dict) -> list[dict]:
        invocation = payload.get("invocation")
        if not isinstance(invocation, dict):
            return []

        call_id = str(payload.get("call_id") or payload.get("id") or _new_uuid())
        item = {
            "id": call_id,
            "type": "mcp_tool_call",
            "server": invocation.get("server") or "",
            "tool": invocation.get("tool") or "unknown",
            "arguments": invocation.get("arguments") or {},
        }

        result = payload.get("result")
        if isinstance(result, dict):
            if isinstance(result.get("Ok"), dict):
                item["result"] = result["Ok"]
            elif "Err" in result:
                item["error"] = result["Err"]
            else:
                item["result"] = result

        if call_id in self.emitted_tool_result_ids:
            self._consume_response_tool_state(call_id)
            return []

        rows: list[dict] = []
        tool_parent, _, _ = self._consume_response_tool_state(call_id)
        if tool_parent is None:
            tool_event = self._push_from_native(
                raw_event,
                _normalize_mcp_tool_started(item, self.parent_uuid),
                uuid_suffix=":tool_use",
            )
            rows.extend(tool_event)
            tool_parent = tool_event[-1]["uuid"] if tool_event else self.parent_uuid

        if call_id not in self.emitted_tool_result_ids:
            result_rows = self._push_from_native(
                raw_event,
                _normalize_mcp_tool_completed(item, tool_parent),
                uuid_suffix=":tool_result",
            )
            if result_rows:
                self.emitted_tool_result_ids.add(call_id)
            rows.extend(result_rows)
        return rows

    def _consume_response_tool_state(self, tool_use_id: str) -> tuple[Optional[str], str, bool]:
        tool_parent = self.response_tool_parents.pop(tool_use_id, None)
        tool_name = self.response_tool_names.pop(tool_use_id, "")
        is_agent_tool = tool_use_id in self.response_agent_tool_parents
        self.response_agent_tool_parents.pop(tool_use_id, None)
        return tool_parent, tool_name, is_agent_tool

    def _normalize_response_payload(self, payload: dict) -> list[dict]:
        payload_type = payload.get("type")
        if payload_type == "message":
            event = _normalize_response_item_event(payload, self.parent_uuid)
            event = self._attach_subagent_notification_to_agent(event)
            text = self._assistant_text(event)
            if text and not self._claim_assistant_text(text):
                # Finalized assistant answer already streamed this turn as an
                # event_msg.agent_message; drop the echo. Messages without
                # assistant text (e.g. subagent notifications) pass through.
                return []
            return self._push(event)
        if payload_type == "reasoning":
            return self._push(_normalize_response_item_event(payload, self.parent_uuid))
        if payload_type == "agent_message":
            return self._normalize_response_agent_message(payload)

        if payload_type in ("function_call", "custom_tool_call", "tool_search_call"):
            normalized, tool_use_id = _normalize_response_tool_call(payload, self.parent_uuid)
            normalized["uuid"] = _response_item_uuid(self.parent_uuid, payload, ":tool_use")
            self.response_tool_parents[tool_use_id] = normalized["uuid"]
            content = (normalized.get("message") or {}).get("content")
            if isinstance(content, list) and content and isinstance(content[0], dict):
                name = content[0].get("name")
                if isinstance(name, str):
                    self.response_tool_names[tool_use_id] = name
            if self._is_agent_tool_call(normalized):
                self.response_agent_tool_parents[tool_use_id] = normalized["uuid"]
            return self._push(normalized)

        if payload_type in (
            "function_call_output",
            "custom_tool_call_output",
            "tool_search_output",
        ):
            tool_use_id = str(payload.get("call_id") or payload.get("id") or "")
            tool_parent, tool_name, is_agent_tool = self._consume_response_tool_state(
                tool_use_id,
            )
            if tool_use_id and tool_use_id in self.emitted_tool_result_ids:
                return []
            tool_parent = tool_parent or self.parent_uuid
            normalized, _ = _normalize_response_tool_result(payload, tool_parent)
            normalized["uuid"] = _response_item_uuid(self.parent_uuid, payload, ":tool_result")
            if is_agent_tool:
                normalized["codex_spawn_agent_result"] = True
            if tool_name == "wait_agent":
                normalized["codex_wait_agent_result"] = True
            self._remember_agent_id(tool_use_id, tool_parent, payload, is_agent_tool=is_agent_tool)
            rows = self._push(normalized)
            if tool_use_id and rows:
                self.emitted_tool_result_ids.add(tool_use_id)
            return rows

        if payload_type == "web_search_call":
            item = _web_search_item_from_payload(payload)
            call_id = item.get("id", "")
            search_key = _web_search_dedupe_key(item)
            if (
                (call_id and call_id in self.seen_web_search_calls)
                or search_key in self.seen_web_search_keys
            ):
                return []
            event = _normalize_web_search(item, self.parent_uuid)
            event["uuid"] = _response_item_uuid(
                self.parent_uuid, payload, ":web_search",
            )
            if call_id:
                self.seen_web_search_calls.add(call_id)
            self.seen_web_search_keys.add(search_key)
            return self._push(event)

        return self._push(_normalize_response_item_event(payload, self.parent_uuid))

    def _normalize_response_agent_message(self, payload: dict) -> list[dict]:
        message_type, body = _codex_agent_message_parts(payload)
        if not body:
            return []
        author = payload.get("author")
        mapping = self._resolve_agent_mapping(author if isinstance(author, str) else "")
        if mapping is not None and message_type == "FINAL_ANSWER":
            tool_use_id, tool_parent = mapping
            normalized, _ = _normalize_response_tool_result(
                {
                    "type": "function_call_output",
                    "call_id": tool_use_id,
                    "output": body,
                },
                tool_parent,
            )
            normalized["uuid"] = _response_item_uuid(self.parent_uuid, payload, ":agent_message")
            normalized["codex_spawn_agent_result"] = True
            return self._push(normalized)

        label = f"Sub-agent {author}" if isinstance(author, str) and author else "Sub-agent"
        if message_type:
            label = f"{label} {message_type}"
        return self._push_from_native(
            payload,
            _normalize_event_msg_text(payload, self.parent_uuid, f"{label}\n\n{body}"),
            uuid_suffix=":agent_message_text",
        )

    def _is_agent_tool_call(self, event: Optional[dict]) -> bool:
        content = ((event or {}).get("message") or {}).get("content")
        if not isinstance(content, list) or not content:
            return False
        block = content[0]
        return isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Agent"

    def _remember_agent_id(
        self,
        tool_use_id: str,
        tool_parent: str,
        payload: dict,
        *,
        is_agent_tool: Optional[bool] = None,
    ) -> None:
        if is_agent_tool is None:
            is_agent_tool = tool_use_id in self.response_agent_tool_parents
        if not is_agent_tool:
            return
        output = payload.get("output")
        if not isinstance(output, str):
            return
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            return
        if not isinstance(parsed, dict):
            return
        agent_id = parsed.get("agent_id")
        task_name = parsed.get("task_name")
        for key in (agent_id, task_name):
            if isinstance(key, str) and key:
                mappings = self.response_agent_ids.setdefault(key, [])
                mapping = (tool_use_id, tool_parent)
                if mapping not in mappings:
                    mappings.append(mapping)

    def _resolve_agent_mapping(self, key: str) -> Optional[tuple[str, str]]:
        mappings = self.response_agent_ids.get(key)
        if not mappings or len(mappings) != 1:
            return None
        return mappings[0]

    def _attach_subagent_notification_to_agent(self, event: Optional[dict]) -> Optional[dict]:
        if not event or event.get("type") != "user":
            return event
        content = ((event.get("message") or {}).get("content") or [])
        if not isinstance(content, list) or not content:
            return event
        block = content[0]
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            return event
        tool_use_id = block.get("tool_use_id")
        if not isinstance(tool_use_id, str):
            return event
        mapped = self._resolve_agent_mapping(tool_use_id)
        if not mapped:
            return event
        agent_tool_use_id, agent_parent_uuid = mapped
        block["tool_use_id"] = agent_tool_use_id
        event["parentUuid"] = agent_parent_uuid
        return event

    def _normalize_web_search_payload(self, payload: dict) -> list[dict]:
        item = _web_search_item_from_payload(payload)
        call_id = item.get("id", "")
        search_key = _web_search_dedupe_key(item)
        if (
            (call_id and call_id in self.seen_web_search_calls)
            or search_key in self.seen_web_search_keys
        ):
            return []
        normalized = _normalize_web_search(item, self.parent_uuid)
        if call_id:
            self.seen_web_search_calls.add(call_id)
        self.seen_web_search_keys.add(search_key)
        return self._push(normalized)

    def _normalize_item_event(self, event_type: str, item: dict) -> list[dict]:
        item_type = item.get("type", "")
        item_id = item.get("id", "")
        item = _attach_collab_parent_from_thread(item, self.collab_thread_parents)
        if item_type == "collab_agent_tool_call":
            _remember_collab_receivers(item, self.collab_thread_parents)
        item_key = item_id
        if not item_key:
            item_key = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)

        def item_uuid(suffix: str = "") -> str:
            return _stable_uuid(
                self.namespace,
                f"item:{item_type}:{event_type}:{item_key}{suffix}",
            )

        if item_type == "todo_list":
            td = _normalize_todo_list(
                item,
                self.parent_uuid,
                _stable_uuid(self.namespace, item_id or "todo"),
            )
            if td is None:
                return []
            if self._line_timestamp:
                td["timestamp"] = self._line_timestamp
            return [td]

        normalized = None
        out: list[dict] = []

        if event_type == "item.completed":
            if item_type == "agent_message":
                normalized = _normalize_agent_message(
                    item, self.parent_uuid, event_uuid=item_uuid(),
                )
            elif item_type == "reasoning":
                normalized = _normalize_reasoning(
                    item, self.parent_uuid, event_uuid=item_uuid(),
                )
            elif item_type == "command_execution":
                tool_use_uuid = self.started_items.pop(item_id, self.parent_uuid)
                normalized = {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": item_id,
                            "content": item.get("aggregated_output", "") or "",
                        }],
                    },
                    "uuid": item_uuid(":result"),
                    "parentUuid": tool_use_uuid,
                    "timestamp": datetime.now().isoformat(),
                }
            elif item_type == "file_change":
                tool_use, tool_result = _normalize_file_change(item, self.parent_uuid)
                out.extend(self._push(tool_use))
                normalized = tool_result
            elif item_type == "mcp_tool_call":
                normalized = _normalize_mcp_tool_completed(item, self.parent_uuid)
            elif item_type == "collab_agent_tool_call":
                tool_use_uuid = self.started_items.pop(item_id, self.parent_uuid)
                normalized = _normalize_collab_agent_completed(item, tool_use_uuid)
            elif item_type == "web_search":
                for event in _normalize_web_search_events(item, self.parent_uuid):
                    out.extend(self._push(event))
                return out
            elif item_type == "error":
                normalized = _normalize_error_item(item, self.parent_uuid)
                normalized["uuid"] = item_uuid()

        elif event_type == "item.started":
            if item_type == "command_execution":
                normalized = _normalize_command_started(item, self.parent_uuid)
                if normalized:
                    normalized["uuid"] = item_uuid(":tool_use")
                    self.started_items[item_id] = normalized["uuid"]
            elif item_type == "mcp_tool_call":
                normalized = _normalize_mcp_tool_started(item, self.parent_uuid)
                if normalized:
                    normalized["uuid"] = item_uuid(":tool_use")
                    self.started_items[item_id] = normalized["uuid"]
            elif item_type == "collab_agent_tool_call":
                normalized = _normalize_collab_agent_started(item, self.parent_uuid)
                if normalized:
                    normalized["uuid"] = item_uuid(":tool_use")
                    self.started_items[item_id] = normalized["uuid"]

        elif event_type == "item.updated" and item_type == "collab_agent_tool_call":
            if item_id not in self.started_items:
                normalized = _normalize_collab_agent_started(item, self.parent_uuid)
                if normalized:
                    normalized["uuid"] = item_uuid(":tool_use")
                    self.started_items[item_id] = normalized["uuid"]

        out.extend(self._push(normalized))
        return out


def normalize_rollout_file(
    path: Path,
    *,
    start_byte: int,
    namespace: str,
) -> tuple[list[dict], Optional[int]]:
    """Replay a rollout file off-line (recovery / re-digest). Returns the
    wrapped event list and the last seen model context window."""
    normalizer = CodexRolloutNormalizer(namespace=namespace)
    wrapped: list[dict] = []
    try:
        with path.open("rb") as f:
            f.seek(max(0, start_byte))
            for raw in f:
                line = raw.decode("utf-8", errors="replace")
                for event in normalizer.normalize_line(line):
                    wrapped.append({"type": "agent_message", "data": event})
    except OSError:
        logger.exception("failed reading codex rollout %s", path)
    return wrapped, normalizer.context_window


class CodexRolloutTailer:
    _POLL_INTERVAL = 0.05

    def __init__(
        self,
        *,
        path: Path,
        start_byte: int,
        namespace: str,
        dispatch: Callable[[dict], Any],
        on_cursor_advance: Optional[Callable[[int], None]] = None,
        on_context_update: Optional[Callable[[Optional[int], Optional[int]], Any]] = None,
        on_terminal_update: Optional[Callable[[bool], Any]] = None,
    ) -> None:
        self.path = path
        self.namespace = namespace
        self.dispatch = dispatch
        self.on_cursor_advance = on_cursor_advance
        self.on_context_update = on_context_update
        self.on_terminal_update = on_terminal_update
        self.processed_byte = max(0, int(start_byte))
        self._stop_event = asyncio.Event()
        self._drain_lock = asyncio.Lock()
        self.normalizer = CodexRolloutNormalizer(namespace=self.namespace)

    def stop(self) -> None:
        self._stop_event.set()

    async def drain_available(self) -> bool:
        async with self._drain_lock:
            return await self._drain_available_locked()

    async def _drain_available_locked(self) -> bool:
        emitted = False
        normalizer = self.normalizer
        lines = await asyncio.to_thread(
            self._read_available_lines,
            self.processed_byte,
        )
        for raw, cursor in lines:
            before = (normalizer.context_window, normalizer.context_tokens)
            terminal_state = None
            try:
                terminal_state = _codex_terminal_state(
                    json.loads(raw.decode("utf-8", errors="replace"))
                )
            except json.JSONDecodeError:
                terminal_state = None
            for event in normalizer.normalize_line(
                raw.decode("utf-8", errors="replace")
            ):
                await self._dispatch(event)
            after = (normalizer.context_window, normalizer.context_tokens)
            if after != before and self.on_context_update is not None:
                res = self.on_context_update(after[0], after[1])
                if inspect.isawaitable(res):
                    await res
            self.processed_byte = cursor
            emitted = True
            if self.on_cursor_advance is not None:
                self.on_cursor_advance(self.processed_byte)
            if terminal_state is not None and self.on_terminal_update is not None:
                res = self.on_terminal_update(terminal_state)
                if inspect.isawaitable(res):
                    await res
        return emitted

    def _read_available_lines(self, start_byte: int) -> list[tuple[bytes, int]]:
        lines: list[tuple[bytes, int]] = []
        try:
            with self.path.open("rb") as f:
                f.seek(start_byte)
                while True:
                    start = f.tell()
                    raw = f.readline()
                    if not raw:
                        break
                    if not raw.endswith(b"\n"):
                        break
                    cursor = f.tell()
                    lines.append((raw, cursor))
                    if cursor <= start:
                        break
        except OSError:
            pass
        return lines

    async def run(self) -> None:
        while not self._stop_event.is_set():
            emitted = await self.drain_available()
            if not emitted:
                sleep_task = asyncio.create_task(asyncio.sleep(self._POLL_INTERVAL))
                stop_task = asyncio.create_task(self._stop_event.wait())
                try:
                    await asyncio.wait(
                        [sleep_task, stop_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for task in (sleep_task, stop_task):
                        if not task.done():
                            task.cancel()

    async def _dispatch(self, event: dict) -> None:
        result = self.dispatch(event)
        if inspect.isawaitable(result):
            await result
