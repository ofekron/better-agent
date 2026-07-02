"""Per-session provenance log: what the agent ran and WHY.

One append-only ``provenance.jsonl`` per session under ``ba_home``. Each row
is a tool invocation extracted from a normalized assistant event:

    {uuid, tool, input, why, ts, msg_id}

``why`` is the assistant's reasoning that immediately preceded the tool call
(the thinking / text blocks in the same message content list) — the model's
stated intent for running it.

Idempotency: append dedups by the tool_use id (``toolu_...``, unique per
call). The dedup set ``_seen`` is HYDRATED FROM THE EXISTING FILE on first
touch of a session in this process, so it survives a backend restart — this
is what makes the log idempotent under crash-recovery replay, which re-runs
the SAME events through ``apply_event`` with ``live=True`` (NOT live=False —
the recovery funnel is live, see run_recovery.py). The in-memory set alone
would reset on restart and double-write every recovered tool row; hydration
closes that hole. The caller's ``live`` gate only avoids the redundant
reconcile path (events.jsonl re-read), not recovery.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Optional

from paths import ba_home

_lock = threading.Lock()
# Per-sid set of tool_use ids already on disk. Lazily HYDRATED from the
# existing provenance.jsonl the first time a sid is touched this process, so
# dedup survives a restart (crash-recovery replays the same events through
# apply_event with live=True; without hydration every recovered tool row
# would double-write).
_seen: dict[str, set] = {}


def _dir() -> str:
    d = os.path.join(ba_home(), "provenance")
    os.makedirs(d, exist_ok=True)
    return d


def _path(app_session_id: str) -> str:
    safe = os.path.basename(app_session_id)
    if safe != app_session_id or not safe or safe in (".", ".."):
        raise ValueError(f"unsafe app_session_id: {app_session_id!r}")
    return os.path.join(_dir(), f"{safe}.jsonl")


def extract(normalized: dict, *, backend_msg_id: Optional[str] = None) -> list[dict]:
    """Pull provenance rows from one normalized assistant event: every
    tool_use block + the reasoning (thinking/text) that preceded it."""
    data = normalized.get("data") or {}
    msg = data.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    ts = normalized.get("timestamp") or data.get("timestamp")
    provider_msg_id = msg.get("id") or normalized.get("uuid")
    msg_id = backend_msg_id or provider_msg_id
    why_parts: list[str] = []
    rows: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype in ("thinking", "text"):
            txt = block.get("thinking") or block.get("text") or ""
            if txt:
                why_parts.append(txt)
        elif btype == "tool_use":
            rows.append({
                "uuid": block.get("id") or normalized.get("uuid"),
                "tool": block.get("name"),
                "input": block.get("input"),
                "why": " ".join(why_parts).strip()[:2000],
                "ts": ts,
                "msg_id": msg_id,
                "provider_msg_id": provider_msg_id,
            })
    return rows


def _hydrate_seen(app_session_id: str) -> set:
    """Return the dedup set for a session, loading existing tool_use ids
    from disk on first access (so dedup survives a backend restart). MUST be
    called with ``_lock`` held."""
    seen = _seen.get(app_session_id)
    if seen is not None:
        return seen
    seen = set()
    try:
        with open(_path(app_session_id), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    uid = json.loads(line).get("uuid")
                except (ValueError, AttributeError):
                    # malformed JSON, or a valid-JSON non-dict scalar line
                    continue
                if uid is not None:
                    seen.add(uid)
    except (FileNotFoundError, OSError, ValueError):
        pass
    _seen[app_session_id] = seen
    return seen


def record(app_session_id: str, rows: list[dict]) -> int:
    """Append new provenance rows (deduped by uuid). Returns the count
    actually written."""
    if not rows:
        return 0
    written = 0
    with _lock:
        seen = _hydrate_seen(app_session_id)
        lines = []
        for r in rows:
            uid = r.get("uuid")
            if uid in seen:
                continue
            seen.add(uid)
            lines.append(json.dumps(r))
        if not lines:
            return 0
        with open(_path(app_session_id), "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        written = len(lines)
    return written


def record_from_event(
    app_session_id: str,
    normalized: dict,
    *,
    backend_msg_id: Optional[str] = None,
) -> int:
    """Extract + append in one call. Returns rows written (0 if none)."""
    return record(app_session_id, extract(normalized, backend_msg_id=backend_msg_id))


def read(app_session_id: str, *, limit: Optional[int] = None) -> list[dict]:
    """Return the session's provenance rows (oldest first). ``limit`` keeps
    only the most-recent N."""
    try:
        with open(_path(app_session_id), encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except (FileNotFoundError, ValueError, OSError):
        return []
    if limit is not None and len(rows) > limit:
        return rows[-limit:]
    return rows


# ── File-change projection ───────────────────────────────────────────
# The Changes right-panel shows every file edit made in a session plus the
# reasoning ("why") that preceded it. The authoritative source is the
# provenance log above; this is a disposable read projection (filter +
# normalize), never a second source of truth — re-derived on every fetch.
#
# Tool names are the union across providers (Claude / Codex / Gemini); the
# agents all normalize to Claude-shaped tool_use events before provenance.

_WRITE_TOOLS = {"Write", "write_file", "create_file"}
_EDIT_TOOLS = {"Edit", "edit_file", "MultiEdit", "multi_edit", "NotebookEdit"}


def _is_patch_tool(tool: Optional[str]) -> bool:
    return bool(tool) and (tool == "apply_patch" or tool.endswith(".apply_patch"))


def normalize_change(row: dict) -> Optional[dict]:
    """Project one provenance row into a file-change shape, or ``None`` if
    the row is not a file edit. Output::

        {uuid, tool, kind, file_path, edits:[{old_string,new_string}],
         why, ts, msg_id}

    ``kind`` is one of ``create`` | ``edit`` | ``patch``. ``edits`` carries
    old→new pairs (create ⇒ one pair with empty old; patch ⇒ raw patch text
    as the new side, file_path best-effort)."""
    tool = row.get("tool")
    inp = row.get("input") if isinstance(row.get("input"), dict) else {}

    def _path(*keys: str) -> Optional[str]:
        for k in keys:
            v = inp.get(k)
            if isinstance(v, str) and v:
                return v
        return None

    edits: list[dict] = []
    kind: Optional[str] = None
    file_path: Optional[str] = None

    if tool in _WRITE_TOOLS:
        kind = "create"
        file_path = _path("file_path", "path", "filename")
        edits = [{"old_string": "", "new_string": inp.get("content") or inp.get("file_text") or ""}]
    elif tool in _EDIT_TOOLS:
        kind = "edit"
        file_path = _path("file_path", "path", "notebook_path")
        multi = inp.get("edits")
        if isinstance(multi, list):
            edits = [
                {"old_string": e.get("old_string") or "", "new_string": e.get("new_string") or ""}
                for e in multi
                if isinstance(e, dict)
            ]
        else:
            edits = [{"old_string": inp.get("old_string") or "", "new_string": inp.get("new_string") or ""}]
    elif _is_patch_tool(tool):
        kind = "patch"
        file_path = _path("file_path", "path")
        edits = [{"old_string": "", "new_string": inp.get("patch") or inp.get("input") or ""}]

    if kind is None:
        return None
    if not edits:
        return None
    return {
        "uuid": row.get("uuid"),
        "tool": tool,
        "kind": kind,
        "file_path": file_path,
        "edits": edits,
        "why": row.get("why") or "",
        "ts": row.get("ts"),
        "msg_id": row.get("msg_id"),
    }


def read_file_changes(app_session_id: str) -> list[dict]:
    """All file edits in a session (oldest first), normalized for the
    Changes panel. Non-edit tool calls are dropped."""
    return [
        c for c in (
            normalize_change(r) for r in read(app_session_id)
        )
        if c is not None
    ]


def _user_prompt_text(msg: dict) -> str:
    """Extract a user message's prompt text (content may be a string or a
    list of content blocks, as the SDK delivers it)."""
    c = msg.get("content")
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):
        parts = [
            b.get("text", "")
            for b in c
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return " ".join(p for p in parts if p).strip()
    return ""


def _parse_event_ts(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    raw = value
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt.astimezone(timezone.utc)


def _turn_for_ts(turn_starts: list[tuple[int, datetime]], ts: Optional[datetime]) -> Optional[int]:
    if ts is None:
        return None
    matched: Optional[int] = None
    for turn_index, start in turn_starts:
        if start > ts:
            break
        matched = turn_index
    return matched


def group_changes_by_turn(messages: list, changes: list) -> list[dict]:
    """Group flat change rows by the user→assistant turn that produced them.

    A turn starts at each ``role == "user"`` message and owns every following
    assistant message until the next user message. Each change's ``msg_id`` is
    the assistant message id (set in :func:`extract`), so we map assistant
    msg_id → turn index. Returns chronological::

        [{turn_index, user_prompt, ts, changes: [...]}, ...]

    Changes whose ``msg_id`` isn't found in the render tree land in a trailing
    ``turn_index = -1`` ("ungrouped") bucket — e.g. edits from a worker fork
    whose panel isn't in the root message list."""
    turn_of_msg: dict = {}      # assistant msg id -> turn index
    prompts: dict = {}          # turn index -> user prompt
    turn_starts: list[tuple[int, datetime]] = []
    ti = -1
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "user":
            ti += 1
            prompts[ti] = _user_prompt_text(m)
            ts = _parse_event_ts(m.get("timestamp"))
            if ts is not None:
                turn_starts.append((ti, ts))
        elif role == "assistant":
            mid = m.get("id")
            if mid is not None:
                # Assistant msg before any user msg -> turn 0.
                turn_of_msg[mid] = ti if ti >= 0 else 0

    buckets: dict = {}
    for c in changes:
        key = turn_of_msg.get(c.get("msg_id"))
        if key is None:
            key = _turn_for_ts(turn_starts, _parse_event_ts(c.get("ts")))
        if key is None:
            key = -1  # ungrouped
        buckets.setdefault(key, []).append(c)

    ordered = sorted(k for k in buckets if k >= 0)
    result = []
    for t in ordered:
        chs = buckets.get(t, [])
        result.append({
            "turn_index": t,
            "user_prompt": prompts.get(t, ""),
            "ts": chs[0].get("ts") if chs else None,
            "changes": chs,
        })
    if -1 in buckets:
        chs = buckets[-1]
        result.append({
            "turn_index": -1,
            "user_prompt": "",
            "ts": chs[0].get("ts") if chs else None,
            "changes": chs,
        })
    return result
