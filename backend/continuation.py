from __future__ import annotations

import json
from typing import Iterable, Optional

from prompt_templates import render_prompt
from runs_dir import iter_run_dirs
from session_store import session_file_path

CONTEXT_OVERFLOW_ERROR = "context_window_exceeded"

_OVERFLOW_MARKERS = (
    "context_window_exceeded",
    "model_context_window_exceeded",
    "context_length_exceeded",
    "context length exceeded",
    "context window exceeded",
    "context window",
    "context limit",
    "token limit",
    "maximum context",
    "maximum tokens",
)


def normalize_context_overflow_error(message: Optional[str]) -> Optional[str]:
    if not message:
        return None
    lower = message.lower()
    if any(marker in lower for marker in _OVERFLOW_MARKERS):
        return CONTEXT_OVERFLOW_ERROR
    if "context" in lower and any(word in lower for word in ("exceed", "length", "limit", "window")):
        return CONTEXT_OVERFLOW_ERROR
    return None


def is_context_overflow_error(message: Optional[str]) -> bool:
    return normalize_context_overflow_error(message) is not None


def build_continuation_prompt(
    *,
    prompt: str,
    app_session_id: str,
    continuation_chain: Iterable[str],
    reason: str = "context_exceeded",
) -> str:
    provider_session_ids = [
        str(item).strip()
        for item in continuation_chain
        if str(item).strip()
    ]
    provider_session_ids_block = ""
    if provider_session_ids:
        provider_session_ids_block = (
            "\n\nPrevious provider session ids: " + ", ".join(provider_session_ids)
        )
    provider_session_paths = _provider_session_paths(provider_session_ids)
    provider_session_paths_block = ""
    if provider_session_paths:
        provider_session_paths_block = "\n\nPrevious provider session id paths:\n" + "\n".join(
            f"- {sid}: {path}" for sid, path in provider_session_paths
        )

    context_message = "Context window was exceeded"
    continuity_message = (
        "You are now in a fresh subprocess of the same Better Agent session "
        "— your prior context is not in this window."
    )
    if reason == "selector_changed":
        context_message = "Session provider or model changed"
    elif reason == "agent_requested":
        context_message = "The agent requested a fresh context window"
    elif reason == "moved_project":
        context_message = "This session was moved here from another project"
        continuity_message = (
            "You are in a new Better Agent session that continues the moved "
            "session — its context is not in this window."
        )

    return render_prompt(
        "continuation/context_exceeded.md",
        {
            "context_message": context_message,
            "continuity_message": continuity_message,
            "app_session_id": app_session_id,
            "app_session_file_path": session_file_path(app_session_id),
            "provider_session_ids_block": provider_session_ids_block,
            "provider_session_paths_block": provider_session_paths_block,
            "prompt": prompt,
        },
    ).rstrip("\n")


def _provider_session_paths(provider_session_ids: Iterable[str]) -> list[tuple[str, str]]:
    wanted = [sid for sid in provider_session_ids if sid]
    wanted_set = set(wanted)
    if not wanted_set:
        return []
    found: dict[str, tuple[float, str]] = {}
    for run_dir in iter_run_dirs() or ():
        state_path = run_dir / "backend_state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sid = str(state.get("session_id") or "").strip()
        path = str(state.get("jsonl_path") or "").strip()
        if sid in wanted_set and path:
            try:
                mtime = state_path.stat().st_mtime
            except OSError:
                continue
            existing = found.get(sid)
            if existing is None or existing[0] <= mtime:
                found[sid] = (mtime, path)
    return [(sid, found[sid][1]) for sid in wanted if sid in found]
