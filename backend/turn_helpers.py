"""Pure helpers shared between `Coordinator` and `TurnManager`.

These constants and functions live OUTSIDE both `orchestrator.py` and
`turn_manager.py` so the two can import from a common neutral module
without forming a circular dependency once `Coordinator` holds a
`TurnManager` instance and `TurnManager`'s relocated method bodies
reference them at module scope.

Nothing in this file is stateful; nothing depends on `Coordinator` or
`TurnManager`. Pure-function classifiers + the cli_prompt nudge.

Categories:
  - Rate-limit detection (typed code + free-text patterns)
  - Transient error detection (network / 5xx / overloaded — retry-able)
  - Stale-session detection (`--resume` target not found)
  - Open-todo cli_prompt reminder
"""

import html
import logging
import re
from typing import Optional

from event_shape import (
    extract_output_text as _extract_output_text,
    strip_synthetic_events as _strip_synthetic_events,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rate-limit detection.
# ---------------------------------------------------------------------------
_RATE_LIMIT_TEXT_RE = re.compile(
    r"API Error:\s*429"
    r"|RESOURCE_EXHAUSTED"
    r"|out of (free )?quota"
    r"|exhausted your capacity"
    r"|quota will reset",
    re.IGNORECASE | re.MULTILINE,
)
_RATE_LIMIT_ERROR_SUBSTRINGS = (
    "rate_limit",
    "resource_exhausted",
    "out of free quota",
    "exhausted your capacity",
    "429",
    "quota",
)


def _is_rate_limit_attempt(error: Optional[str], events: list[dict]) -> bool:
    if error:
        low = error.lower()
        if any(s in low for s in _RATE_LIMIT_ERROR_SUBSTRINGS):
            return True
    extracted = _extract_output_text(_strip_synthetic_events(events))
    if extracted and _RATE_LIMIT_TEXT_RE.search(extracted):
        return True
    return False


# ---------------------------------------------------------------------------
# Transient error detection.
# ---------------------------------------------------------------------------
_TRANSIENT_ERROR_SUBSTRINGS = (
    "econnreset",
    "connection reset",
    "socket connection was closed unexpectedly",
    "server_error",
    "internal server error",
    "500",
    "503",
    "529",
    "overloaded",
    "unavailable",
    "connection refused",
    "socket hang up",
    "etimedout",
    "timed out",
    "cli connection error",
    "processerror",
    "unexpected end of stream",
)
_TRANSIENT_ERROR_TEXT_RE = re.compile(
    r"ECONNRESET"
    r"|connection (was )?reset"
    r"|socket (hang up|closed unexpectedly)"
    r"|server(_| )error"
    r"|internal server error"
    r"|500\s"
    r"|503\s"
    r"|529\s"
    r"|overloaded"
    r"|unavailable"
    r"|timed? ?out"
    r"|etimedout",
    re.IGNORECASE | re.MULTILINE,
)
_NON_TRANSIENT_ERROR_SUBSTRINGS = (
    "codex app-server request timed out: initialize",
)
_TRANSIENT_MAX_ATTEMPTS = 10
_TRANSIENT_BASE_WAIT_S = 5.0
_TRANSIENT_MAX_WAIT_S = 60.0
# Rate-limit retries are bounded too: an exhausted subscription window /
# quota must terminate at the cap with the real error instead of
# sleep-looping forever (the rate-limit branch has no per-provider cap).
_RATE_LIMIT_MAX_ATTEMPTS = 5


def _is_transient_error(error: Optional[str], events: list[dict]) -> bool:
    """Detect transient network/server errors that should auto-retry."""
    if error:
        low = error.lower()
        if any(s in low for s in _NON_TRANSIENT_ERROR_SUBSTRINGS):
            return False
        if any(s in low for s in _TRANSIENT_ERROR_SUBSTRINGS):
            return True
    extracted = _extract_output_text(_strip_synthetic_events(events))
    if extracted and _TRANSIENT_ERROR_TEXT_RE.search(extracted):
        return True
    return False


# ---------------------------------------------------------------------------
# Stale-session detection.
# ---------------------------------------------------------------------------
_STALE_SESSION_ERROR_SUBSTRINGS = (
    "invalid session identifier",
    "session not found",
    "could not find session",
    "--list-sessions",
    "no session found",
)


def _is_stale_session_error(error: str) -> bool:
    low = error.lower()
    return any(s in low for s in _STALE_SESSION_ERROR_SUBSTRINGS)


# ---------------------------------------------------------------------------
# Session todo cli_prompt reminder.
# ---------------------------------------------------------------------------
_UNFINISHED_TODOS_OPEN = "<bc-todo-reminder>"
_UNFINISHED_TODOS_CLOSE = "</bc-todo-reminder>"


def _session_work_items(session: dict) -> list[dict]:
    items: list[dict] = []
    seen: dict[str, int] = {}
    for field in ("current_todos", "current_tasks"):
        for item in session.get(field) or []:
            if not isinstance(item, dict):
                continue
            content = " ".join(str(item.get("content") or "Untitled todo").split())
            key = content.casefold()
            normalized = {**item, "content": content}
            if key in seen:
                existing = items[seen[key]]
                if existing.get("status") != "completed" and normalized.get("status") == "completed":
                    items[seen[key]] = normalized
                continue
            seen[key] = len(items)
            items.append(normalized)
    return items


def _append_todo_reminder(cli_prompt: str, session: dict) -> str:
    """Append tagged session todo state to `cli_prompt`.

    Caller-side gates (user_initiated, empty-prompt) decide whether
    to call this. Empty and all-completed lists leave the prompt unchanged.
    """
    todos = _session_work_items(session)
    unfinished = [
        todo for todo in todos
        if todo.get("status") != "completed"
    ]
    if not unfinished:
        return cli_prompt

    lines = []
    for todo in todos:
        status = html.escape(
            " ".join(str(todo.get("status") or "pending").split())
        )
        content = html.escape(
            " ".join(str(todo.get("content") or "Untitled todo").split())
        )
        lines.append(f"- [{status}] {content}")
    unfinished_block = (
        f"{_UNFINISHED_TODOS_OPEN}\n"
        "Current todo state from this session:\n"
        + "\n".join(lines)
        + f"\n{_UNFINISHED_TODOS_CLOSE}"
    )

    return cli_prompt.rstrip() + "\n\n" + unfinished_block
