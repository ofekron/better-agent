"""Cross-provider TODO and task extraction from session events.

Two independent funnels:
  1. `extract_todos_from_normalized` — TodoWrite (Claude full-list
     snapshot) and `update_topic` (Gemini single-topic delta, mapped
     to "TodoWrite" by `runner_gemini`).
  2. `extract_tasks_from_normalized` — Claude Code `TaskCreate` /
     `TaskUpdate` (individual item ops).

Operates on a `_normalize_for_render`-normalized event so the
manager_event vs raw-agent_message asymmetry is already collapsed.

Idempotence / replay safety:
  - Claude shape UNION-merge: items in the new snapshot update matching
    current items (by content); items NOT in the snapshot are kept;
    new items are appended. Accumulates across all TodoWrite calls so
    completed items from earlier phases are never lost.
  - Gemini shape dedupes by `source_id` (stable across replay because
    real samples confirm Gemini supplies `update_topic_<ts>_<n>` IDs;
    defensive content-hash fallback covers any future divergence).
  - TaskCreate dedupes by content hash — same subject → no duplicate.
  - TaskUpdate is idempotent on replay (same status transition is a no-op).

Purity invariant (locked by `test_todos_extraction`):
  - NEVER mutates any item in `current`. Always builds fresh dicts.
  - Callers can pass a shallow-copy snapshot without fear of leaks.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional


# Real Gemini sample IDs: `update_topic_1780096408174_0`.
# Pattern lock — only these get trusted as-is. Anything else (e.g. a
# `_new_uuid()` fallback in `runner_gemini._normalize_tool_use`) is
# replaced with a deterministic content-hash so replays don't diverge.
_GEMINI_TOOL_ID_PATTERN = re.compile(r"^update_topic_\d+_\d+$")


def _content_hash_source_id(inp: dict) -> Optional[str]:
    """Deterministic source_id from the Gemini input fields. Same input
    → same hash → replay-safe dedup. Returns None if the input is not
    JSON-serializable (skip the event rather than crash the hot path)."""
    try:
        blob = json.dumps(inp, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(blob).hexdigest()[:16]


def _normalize_claude_todo(t: dict) -> dict:
    """Build a fresh TodoItem from a Claude `input.todos[*]` entry.
    Never mutates `t` — produces a new dict."""
    return {
        "content": t.get("content", ""),
        "status": t.get("status", "pending"),
        "activeForm": t.get("activeForm"),
    }


def _apply_gemini_delta(
    current: list, inp: dict, source_id: str,
) -> list:
    """Build a new list reflecting a single Gemini `update_topic` call.

    Dedup-by-source_id: if `source_id` already present, replace the
    matching entry in place (handles streaming mutations on the same
    tool_id). Else: mark prior Gemini-OWNED `in_progress` items
    (items that themselves carry a `source_id`) → `completed`, then
    append the new item.

    Scoping the prior-completion to source_id-bearing items prevents
    cross-pollination with Claude TodoWrite output: a Claude
    `in_progress` (no source_id, authoritative status) MUST NOT be
    silently demoted by a subsequent Gemini `update_topic` arriving
    on an interleaved provider stream.

    Never mutates `current` or its items.
    """
    new_item = {
        "content": inp.get("title") or inp.get("strategic_intent") or "(untitled)",
        "status": "in_progress",
        "activeForm": inp.get("summary"),
        "source_id": source_id,
    }

    for idx, item in enumerate(current):
        if item.get("source_id") == source_id:
            out = list(current)
            # Re-emission of a tool_id that's already in current_todos.
            # Update the content fields (handles Gemini streaming
            # mutations) but PRESERVE `completed` — Gemini sometimes
            # re-emits a previously-finished topic (verified against
            # real session data: 33b3991a re-emits tool_ids seq>400
            # that completed at seq<395). Downgrading those back to
            # `in_progress` would mis-render the active focus.
            preserved_status = (
                "completed" if item.get("status") == "completed"
                else new_item["status"]
            )
            out[idx] = {**new_item, "status": preserved_status}
            return out

    out = []
    for item in current:
        if item.get("status") == "in_progress" and item.get("source_id"):
            out.append({**item, "status": "completed"})
        else:
            out.append(dict(item))
    out.append(new_item)
    return out


# ── Shared helpers ───────────────────────────────────────────────

def _find_tool_use_block(normalized: dict, name: str) -> Optional[dict]:
    """Locate the first `tool_use` block with the given name inside a
    normalized event. Returns the block dict or None."""
    data = normalized.get("data") or {}
    if not isinstance(data, dict):
        return None
    msg = data.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        if block.get("name") != name:
            continue
        return block
    return None


def _find_todowrite_block(normalized: dict) -> Optional[dict]:
    return _find_tool_use_block(normalized, "TodoWrite")


# ── TodoWrite extraction (todos) ────────────────────────────────

def _union_merge_claude_todos(current: list, snapshot: list) -> list:
    """UNION-merge a Claude TodoWrite snapshot with the current list.

    Items in `snapshot` that match `current` by content are updated in
    place (snapshot wins on status/activeForm). Items in `current` that
    are NOT in the snapshot are kept with their existing status — this
    preserves completed tasks from earlier phases that Claude dropped.
    New items from the snapshot are appended.

    Never mutates `current` or its items.
    """
    snap_by_content = {item.get("content", ""): item for item in snapshot}
    current_by_content = {item.get("content", ""): item for item in current}

    result = []
    # Keep all current items, updating any that appear in the snapshot.
    for item in current:
        content = item.get("content", "")
        if content in snap_by_content:
            # Snapshot wins for status/activeForm; current keeps extra
            # fields like source_id that snapshot items don't carry.
            result.append({**item, **snap_by_content[content]})
        else:
            # Kept as-is — not in the new snapshot.
            result.append(dict(item))

    # Append snapshot items not already in current.
    for item in snapshot:
        content = item.get("content", "")
        if content not in current_by_content:
            result.append(dict(item))

    return result


def extract_todos_from_normalized(
    normalized: dict, current: list,
) -> Optional[list]:
    """Extract `current_todos` from TodoWrite / update_topic events.

    Args:
        normalized: a `_normalize_for_render`-normalized event.
        current: the session's current `current_todos` list. Treated
            as read-only — never mutated.

    Returns:
        A NEW list to replace `current_todos`, or None when the event
        carries no TodoWrite tool_use.
    """
    block = _find_todowrite_block(normalized)
    if block is None:
        return None

    block_id = block.get("id")
    if not block_id or not isinstance(block_id, str):
        return None

    inp = block.get("input")
    if not isinstance(inp, dict):
        return None

    # Claude shape: full-list UNION-MERGE. Keeps items from earlier
    # phases that Claude dropped — accumulates across all TodoWrite calls.
    todos = inp.get("todos")
    if isinstance(todos, list):
        snapshot = [_normalize_claude_todo(t) for t in todos if isinstance(t, dict)]
        return _union_merge_claude_todos(current, snapshot)

    # Gemini shape: single-topic DELTA.
    if "title" in inp or "strategic_intent" in inp or "summary" in inp:
        if _GEMINI_TOOL_ID_PATTERN.match(block_id):
            source_id = block_id
        else:
            source_id = _content_hash_source_id(inp)
            if source_id is None:
                return None
        return _apply_gemini_delta(current, inp, source_id)

    return None


# ── TaskCreate / TaskUpdate extraction (tasks) ──────────────────

def _apply_task_create(
    current: list, inp: dict, block_id: str,
) -> Optional[list]:
    """Add a new task from a TaskCreate tool_use call.

    Stores `tool_use_id` on the item for later matching with the
    tool_result (which carries the real taskId).

    Dedup-by-content: if an item with matching content already exists
    (from a prior TaskCreate replay), return None (no-op).

    Never mutates `current`.
    """
    subject = inp.get("subject", "")
    if not subject or not isinstance(subject, str):
        return None

    content = subject.strip()
    # Dedup: replay safety — same TaskCreate replayed is a no-op.
    for item in current:
        if item.get("content") == content:
            return None

    new_item: dict = {
        "content": content,
        "status": "pending",
        "activeForm": inp.get("activeForm"),
        "source_id": "tc:" + _content_hash_source_id({"subject": subject}),
        "tool_use_id": block_id,
    }
    return list(current) + [new_item]


def _parse_task_id_from_result(result_content: Any) -> Optional[str]:
    """Extract a taskId from a TaskCreate tool_result's content.

    The content may be:
      - A plain number like "1" or "42"
      - Text like "Created task with ID: 1"
      - Structured JSON (unlikely but handled)
    """
    if result_content is None:
        return None
    # Structured content: list of blocks
    if isinstance(result_content, list):
        for block in result_content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if text:
                    return _parse_task_id_from_result(text)
        return None
    if isinstance(result_content, dict):
        tid = result_content.get("task_id") or result_content.get("taskId")
        if tid:
            return str(tid)
        return None
    if isinstance(result_content, (int, float)):
        return str(int(result_content))
    if isinstance(result_content, str):
        # Try plain number first
        stripped = result_content.strip()
        if stripped.isdigit():
            return stripped
        # Try "ID: N" or "task N" patterns
        import re
        m = re.search(r'(?:ID|id|task)[^\d]*(\d+)', stripped)
        if m:
            return m.group(1)
        # Last resort: first number in the string
        m = re.search(r'(\d+)', stripped)
        if m:
            return m.group(1)
    return None


def _apply_task_result(
    current: list, tool_use_id: str, result_content: Any,
) -> Optional[list]:
    """Process a tool_result for a TaskCreate call.

    Matches by `tool_use_id` on existing items, extracts the taskId
    from the result content, and stamps it on the item as `task_id`.

    Never mutates `current` or its items.
    """
    task_id = _parse_task_id_from_result(result_content)
    if not task_id:
        return None

    for i, item in enumerate(current):
        if item.get("tool_use_id") == tool_use_id and not item.get("task_id"):
            out = list(current)
            out[i] = {**item, "task_id": task_id}
            return out
    return None


def _apply_task_update(current: list, inp: dict) -> Optional[list]:
    """Apply a TaskUpdate to an existing task.

    Matching strategy (in order):
      1. taskId: exact match by `task_id` field (from tool_result).
      2. Content match: if `subject` is provided, find by content.
      3. Status heuristic: find the first item matching the transition
         (pending→in_progress, in_progress→completed).
      4. Delete: remove the matched item.

    Never mutates `current` or its items.
    """
    task_id = inp.get("taskId")
    if not task_id:
        return None

    new_status = inp.get("status")
    new_subject = inp.get("subject")
    new_active_form = inp.get("activeForm")

    out = [dict(item) for item in current]

    # 1. Exact match by task_id
    for i, item in enumerate(out):
        if item.get("task_id") == str(task_id):
            if new_status:
                out[i] = {**out[i], "status": new_status}
            if new_active_form is not None:
                out[i] = {**out[i], "activeForm": new_active_form}
            if new_subject and isinstance(new_subject, str):
                out[i] = {**out[i], "content": new_subject.strip()}
            if new_status == "deleted":
                out.pop(i)
            return out

    # 2. Content match by subject
    if new_subject and isinstance(new_subject, str):
        new_subject = new_subject.strip()
        for i, item in enumerate(out):
            if item.get("content") == new_subject:
                out[i] = dict(item)
                if new_status:
                    out[i]["status"] = new_status
                if new_active_form is not None:
                    out[i]["activeForm"] = new_active_form
                if new_status == "deleted":
                    out.pop(i)
                return out

    # 3. Status heuristic
    if new_status:
        from_status = {
            "in_progress": "pending",
            "completed": "in_progress",
        }.get(new_status)
        if from_status:
            for i, item in enumerate(out):
                if item.get("status") == from_status:
                    out[i] = {**item, "status": new_status}
                    if new_active_form is not None:
                        out[i]["activeForm"] = new_active_form
                    return out
        # Delete: remove the first non-deleted item
        if new_status == "deleted":
            for i, item in enumerate(out):
                if item.get("status") != "deleted":
                    out.pop(i)
                    return out

    return None


def _find_tool_result_blocks(normalized: dict) -> list[dict]:
    """Find all tool_result blocks in a normalized event.

    These come from user-role messages in the raw jsonl that were
    enriched and dispatched as `agent_message` events.
    """
    data = normalized.get("data") or {}
    if not isinstance(data, dict):
        return []
    msg = data.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    return [
        block for block in content
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]


def extract_tasks_from_normalized(
    normalized: dict, current: list,
) -> Optional[list]:
    """Extract `current_tasks` from TaskCreate / TaskUpdate / tool_result
    events.

    Three event types are handled:
      1. TaskCreate tool_use → add new item (stores tool_use_id for
         later linking with the tool_result).
      2. TaskCreate tool_result → extract taskId from result content,
         stamp on the matching item.
      3. TaskUpdate tool_use → update existing item (matches by
         task_id first, then content, then heuristic).

    Args:
        normalized: a `_normalize_for_render`-normalized event.
        current: the session's current `current_tasks` list. Treated
            as read-only — never mutated.

    Returns:
        A NEW list to replace `current_tasks`, or None when the event
        carries no relevant tool_use / tool_result.
    """
    # ── TaskCreate ──────────────────────────────────────────────
    task_create = _find_tool_use_block(normalized, "TaskCreate")
    if task_create is not None:
        inp = task_create.get("input")
        block_id = task_create.get("id", "")
        if isinstance(inp, dict):
            return _apply_task_create(current, inp, block_id)
        return None

    # ── TaskUpdate ──────────────────────────────────────────────
    task_update = _find_tool_use_block(normalized, "TaskUpdate")
    if task_update is not None:
        inp = task_update.get("input")
        if isinstance(inp, dict):
            return _apply_task_update(current, inp)
        return None

    # ── tool_result (for TaskCreate taskId extraction) ──────────
    for tr_block in _find_tool_result_blocks(normalized):
        tool_use_id = tr_block.get("tool_use_id")
        result_content = tr_block.get("content")
        if tool_use_id:
            updated = _apply_task_result(current, tool_use_id, result_content)
            if updated is not None:
                return updated

    return None


# ── Derive helpers (fork / hydration) ───────────────────────────

def derive_current_todos(messages: list) -> list:
    """Walk a sequence of `messages` through the extractor in seq order
    and return the resulting `current_todos`.

    Used by fork mint to re-derive `current_todos` from the COPIED
    message subset (not the parent's running state, which may reflect
    events past the fork point). Deterministic + idempotent on the
    same input — produces the exact same final state the live
    `apply_event` hook would have produced applying the same event
    stream.

    Walks `msg.events` (the flat primary event list). Worker panel
    events (`msg.workers[*].events`) are intentionally NOT walked —
    they belong to worker panels, not the session's current_todos
    (mirrors the worker_event early-return in `apply_event`).

    Never mutates the input messages or their events.
    """
    current: list = []
    for msg in messages or []:
        for event in (msg.get("events") or []):
            new = extract_todos_from_normalized(event, current)
            if new is not None:
                current = new
    return current


def derive_current_tasks(messages: list) -> list:
    """Walk a sequence of `messages` through the task extractor in seq
    order and return the resulting `current_tasks`.

    Same semantics as `derive_current_todos` but for TaskCreate /
    TaskUpdate events.
    """
    current: list = []
    for msg in messages or []:
        for event in (msg.get("events") or []):
            new = extract_tasks_from_normalized(event, current)
            if new is not None:
                current = new
    return current
