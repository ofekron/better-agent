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


def extract(normalized: dict) -> list[dict]:
    """Pull provenance rows from one normalized assistant event: every
    tool_use block + the reasoning (thinking/text) that preceded it."""
    data = normalized.get("data") or {}
    msg = data.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    ts = normalized.get("timestamp") or data.get("timestamp")
    msg_id = msg.get("id") or normalized.get("uuid")
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


def record_from_event(app_session_id: str, normalized: dict) -> int:
    """Extract + append in one call. Returns rows written (0 if none)."""
    return record(app_session_id, extract(normalized))


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
