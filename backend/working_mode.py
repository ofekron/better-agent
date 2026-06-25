"""Working-mode base — shared session lifecycle for ephemeral working
sessions (prompt engineering, file editing, etc.).

Provides common utilities that each mode builds on:
  - mark_working_mode()   — stamp a session with mode + metadata
  - find_working_session() — lookup by mode + match criteria
  - is_working_session()  — check if a session belongs to any working mode
  - cleanup_session()     — delete session + resource paths
  - format_file_comment() — file-anchored comment rendering

Each mode file (prompt_engineer.py, file_editor.py, …) keeps its own
meta-prompt templates and mode-specific logic but delegates session
plumbing here.
"""

import logging
import shutil
from pathlib import Path
from typing import Optional

from event_bus import bus, BusEvent
from session_manager import manager as session_manager

logger = logging.getLogger(__name__)


# ── Session marking ────────────────────────────────────────────────

def mark_working_mode(
    sid: str,
    *,
    mode: str,
    meta: dict,
) -> Optional[dict]:
    """Stamp a session as a working-mode session.

    Sets ``working_mode`` (string) and ``working_mode_meta`` (dict) so
    the frontend + sidebar can filter / route correctly.
    """
    def _do(s: dict) -> None:
        s["working_mode"] = mode
        s["working_mode_meta"] = meta
    return session_manager._run(
        sid,
        _do,
        {"kind": "working_mode_marked", "mode": mode},
        enrich=lambda s: {
            "working_mode": s.get("working_mode"),
            "working_mode_meta": s.get("working_mode_meta"),
        },
    )


# ── Lookup ─────────────────────────────────────────────────────────

def find_working_session(
    mode: str,
    **match: object,
) -> Optional[dict]:
    """Return the first live working session whose mode matches and whose
    ``working_mode_meta`` contains all ``**match`` key-value pairs, or
    ``None``.

    Pre-filters on the in-memory session summary (which already carries
    ``working_mode`` + ``working_mode_meta``) so a full disk-backed
    ``session_manager.get`` runs only for candidates that already match —
    not once per session. This keeps the lookup off the per-session
    root-tree load path that otherwise blocks the caller (and, when the
    caller runs on the event loop, the whole loop). The full-load re-check
    is retained so a stale summary can never produce a false positive."""
    for summary in session_manager.list():
        if summary.get("working_mode") != mode:
            continue
        sm = summary.get("working_mode_meta") or {}
        if not all(sm.get(k) == v for k, v in match.items()):
            continue
        s = session_manager.get(summary["id"]) or {}
        if s.get("working_mode") != mode:
            continue
        m = s.get("working_mode_meta") or {}
        if all(m.get(k) == v for k, v in match.items()):
            return s
    return None


def is_working_session(session_id: str) -> bool:
    """True if the session is any kind of working-mode session."""
    sess = session_manager.get(session_id)
    return bool(sess and sess.get("working_mode"))


def is_working_mode(session_id: str, mode: str) -> bool:
    """True if the session is a specific working mode."""
    sess = session_manager.get(session_id)
    return bool(sess and sess.get("working_mode") == mode)


def should_hide_from_sidebar(session_summary: dict) -> bool:
    """True for any working-mode session that shouldn't appear in the
    main session list. Persistent file-editing sessions (new-session-modal
    entry) are sidebar-visible so the user can navigate back to them."""
    wm = session_summary.get("working_mode")
    if not wm:
        return False
    meta = session_summary.get("working_mode_meta") or {}
    if wm == "file_editing" and meta.get("persistent"):
        return False
    return True


# ── Cleanup ────────────────────────────────────────────────────────

def cleanup_session(
    session_id: str,
    *,
    extra_paths: Optional[list[str]] = None,
) -> bool:
    """Delete a working session record and optional filesystem paths.

    Returns False if the session is already gone or is not a working-mode
    session (idempotent no-op).
    """
    sess = session_manager.get(session_id)
    if sess is None or not sess.get("working_mode"):
        return False

    ok = session_manager.delete(session_id)

    if extra_paths:
        for p in extra_paths:
            path = Path(p)
            if path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)

    return ok


# Modes that own their own `session.parent_deleted` cleanup subscribers
# (e.g. `prompt_engineer` registers one for `"prompt_engineering"`).
# The working_mode catch-all skips these — otherwise both would run and
# the second `session_manager.delete` would no-op but we'd still touch
# the resource paths twice. Keep this list in sync with each mode
# owner's `register_bus_subscribers`.
_OWNED_MODES_WITH_OWN_CLEANUP_SUBSCRIBER: set[str] = {"prompt_engineering"}


def register_bus_subscribers() -> None:
    """A15: catch-all subscriber for `session.parent_deleted`. Cleans
    up working-mode child sessions whose `working_mode` does NOT have
    its own dedicated cleanup subscriber. Mode-specific owners (e.g.
    `prompt_engineer`) subscribe at higher priority and run first; this
    one runs at priority 260 and early-exits on those modes to avoid
    double-cleanup.

    Idempotent — re-binding unsubscribes the previous registration.

    Filesystem cleanup (`Path.unlink`, `shutil.rmtree` in
    `cleanup_session`) is sync and could block the event loop on
    slow disks / large temp dirs, so the actual cleanup runs
    off-loop via `asyncio.to_thread`."""
    import asyncio as _asyncio

    async def _handler(event: BusEvent) -> None:
        mode = event.payload.get("working_mode")
        if not mode or mode in _OWNED_MODES_WITH_OWN_CLEANUP_SUBSCRIBER:
            return
        child_id = event.payload.get("child_session_id")
        if not child_id:
            return
        try:
            await _asyncio.to_thread(cleanup_session, child_id)
        except Exception:
            logger.exception(
                "working_mode subscriber: cleanup_session failed for %s",
                child_id,
            )

    bus.unsubscribe("working_mode_parent_deleted")
    bus.subscribe(
        "session.parent_deleted",
        _handler,
        priority=260,
        name="working_mode_parent_deleted",
    )
    logger.info(
        "event_bus: registered working_mode parent-deleted subscriber",
    )


# ── Comment formatting ─────────────────────────────────────────────

def format_file_comment(
    file_path: str,
    start_line: int,
    end_line: int,
    start_col: int,
    end_col: int,
    comment: str,
) -> str:
    """Render a file-anchored comment as the user-message body.

    The comment text is fenced so a hostile payload can't spoof
    additional anchors.
    """
    if start_line == end_line:
        loc = f"{file_path}:{start_line}:{start_col}-{end_col}"
    else:
        loc = f"{file_path}:{start_line}:{start_col}-{end_line}:{end_col}"
    safe_comment = comment.replace("```", "``​`")
    body = f"Re {loc}\n\n```user-comment\n{safe_comment}\n```"
    return f'<file-comment>\n{body}\n</file-comment>'
