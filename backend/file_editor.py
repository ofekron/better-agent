"""File-editing mode — Better Agent sessions that interactively edit a SET of
real project files with AI assistance.

Lifecycle:
  start    -> one session per project cwd. Idempotent: creates a
              file-editor session for the cwd if none exists, otherwise
              JOINS the existing one (adds the file to its set and
              continues the same agent conversation).
  cleanup  -> caller cancels runners + rearranger first, then delegates
              here to drop the session record. Idempotent. Tears down
              the whole set (every file).

Unlike prompt engineering, file-editor sessions edit the REAL files
in-place (no temp copies). The meta-prompt tells Claude to read, edit,
and iterate on the files based on user instructions.

`persistent` is an upgrade-only flag on the single per-cwd session:
the new-session-modal flavor creates it persistent (sidebar-visible,
no Done button); the project-tree "AI Edit" flavor creates it temporal
(sidebar-hidden, has Done). Whichever exists first for a cwd is the
one subsequent opens join; a temporal session is upgraded to
persistent if a persistent open later targets the same cwd, never
downgraded.
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional
import uuid
from datetime import datetime

from session_manager import manager as session_manager
import config_store
import working_mode
from prompt_templates import render_prompt

logger = logging.getLogger(__name__)

MODE = "file_editing"

_META_PROMPT = render_prompt("file_editor/bootstrap.md")

_ADD_FILE_PROMPT = render_prompt("file_editor/add_file.md")

_EMPTY_SESSION_ASK = (
    "Which file or files do you want to edit? You can pick files with the "
    "file chooser, ask me to create a new file, or describe the files here."
)


def _format_file_list(paths: list) -> str:
    return "\n".join(f"- `{p}`" for p in paths)


def _assert_multifile_meta(meta: dict, sid: str) -> None:
    """Reject the pre-multi-file (single ``file_path``) session shape.

    Schema migration is not supported (project rule) — a legacy
    file-editing session must be discarded, not silently mounted.
    """
    if "file_path" in meta and "file_paths" not in meta:
        raise ValueError(
            f"file_editing session {sid[:8]} uses the legacy single-file "
            "meta shape (`file_path`). Schema migration is not supported — "
            "delete legacy file_editing sessions from "
            "~/.better-claude/sessions/ to start fresh."
        )


def _resume_payload(session: dict) -> dict:
    """Shared response shape for a pure resume (no file added)."""
    meta = session.get("working_mode_meta") or {}
    _assert_multifile_meta(meta, session["id"])
    return {
        "session_id": session["id"],
        "file_paths": list(meta.get("file_paths") or []),
        "original_contents": dict(meta.get("original_contents") or {}),
        "meta_prompt": None,
        "session": session,
        "resumed": True,
    }


async def _baseline(
    node_id: str, file_path: str, cwd: str = ""
) -> dict:
    """Resolve + validate the file (and optional cwd) on the chosen
    node, returning the file's current text as the per-session
    `original_contents` baseline. Single funnel for both local and
    remote sessions — see `node_rpc_handlers._rpc_file_editor_baseline`.

    Returns `{file_path_resolved, cwd_resolved, original_content}`.
    Raises FileNotFoundError / ValueError on missing file / non-dir cwd.
    """
    from node_rpc_handlers import call_local_or_remote
    return await call_local_or_remote(
        node_id,
        "file_editor_baseline",
        {"file_path": file_path, "cwd": cwd},
    )


async def _project_cwd(node_id: str, cwd: str) -> dict:
    from node_rpc_handlers import call_local_or_remote
    return await call_local_or_remote(
        node_id,
        "file_editor_project_cwd",
        {"cwd": cwd},
    )


async def _join_file_set_atomic(
    sid: str,
    node_id: str,
    resolved: str,
    persistent: bool,
) -> tuple[bool, dict]:
    """Add *resolved* to the session's file set + apply the upgrade-only
    persistent flag, atomically under the per-root session lock.

    The read-modify-write of ``working_mode_meta`` MUST happen inside a
    single locked mutation: doing find→read→append→write across separate
    lock acquisitions lets two concurrent opens for the same cwd each
    read the old set and last-writer-wins drop a file silently.

    The baseline read for the newly-added file happens BEFORE the lock
    (one RPC round-trip on a remote node) so the locked region stays
    short and pure-in-memory.

    Returns ``(added, session)`` — *added* is False when the file was
    already in the set (pure resume).
    """
    # Probe first under the lock so we don't pay an RPC roundtrip just
    # to discover the file is already in the set.
    probe: dict = {}

    def _probe(s: dict) -> None:
        meta = dict(s.get("working_mode_meta") or {})
        _assert_multifile_meta(meta, s["id"])
        probe["already_in_set"] = resolved in (meta.get("file_paths") or [])

    session_manager._run(sid, _probe, {"kind": "working_mode_probed", "mode": MODE})

    if probe["already_in_set"]:
        # Pure resume — apply persistent upgrade only.
        outcome: dict = {"added": False}
        def _upgrade(s: dict) -> None:
            meta = dict(s.get("working_mode_meta") or {})
            if persistent and not meta.get("persistent"):
                meta["persistent"] = True
                s["working_mode_meta"] = meta
        sess = session_manager._run(
            sid, _upgrade, {"kind": "working_mode_marked", "mode": MODE}
        )
        return outcome["added"], (sess or session_manager.get(sid) or {})

    # Fetch baseline content for the newly-added file (local or remote).
    try:
        baseline = await _baseline(node_id, resolved)
        orig = baseline["original_content"]
    except Exception:
        orig = ""

    outcome = {"added": True}

    def _do(s: dict) -> None:
        meta = dict(s.get("working_mode_meta") or {})
        _assert_multifile_meta(meta, s["id"])
        file_paths = list(meta.get("file_paths") or [])
        if persistent and not meta.get("persistent"):
            meta["persistent"] = True
        if resolved in file_paths:
            # Lost a race with another concurrent add — treat as pure
            # resume; the other writer already populated original_contents.
            outcome["added"] = False
        else:
            file_paths.append(resolved)
            contents = dict(meta.get("original_contents") or {})
            contents[resolved] = orig
            meta["original_contents"] = contents
        meta["file_paths"] = file_paths
        s["working_mode_meta"] = meta

    sess = session_manager._run(
        sid, _do, {"kind": "working_mode_marked", "mode": MODE}
    )
    return outcome["added"], (sess or session_manager.get(sid) or {})


async def start_empty(
    *,
    cwd: str,
    model: Optional[str] = None,
    provider_id: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    persistent: bool = False,
    node_id: str = "primary",
) -> dict:
    if not cwd:
        raise ValueError("cwd is required for a file-editing session")

    project = await _project_cwd(node_id, cwd)
    project_cwd = project["cwd_resolved"]

    existing = await asyncio.to_thread(
        working_mode.find_working_session, MODE, project_cwd=project_cwd
    )
    if existing:
        if persistent:
            def _upgrade(s: dict) -> None:
                meta = dict(s.get("working_mode_meta") or {})
                _assert_multifile_meta(meta, s["id"])
                if not meta.get("persistent"):
                    meta["persistent"] = True
                    s["working_mode_meta"] = meta

            existing = session_manager._run(
                existing["id"],
                _upgrade,
                {"kind": "working_mode_marked", "mode": MODE},
            ) or existing
        return _resume_payload(existing)

    name = f"✏️ Edit — {Path(project_cwd).name}"
    session = session_manager.create(
        name=name,
        model=model,
        cwd=project_cwd,
        orchestration_mode="native",
        source="web",
        provider_id=provider_id,
        reasoning_effort=reasoning_effort,
        node_id=node_id,
    )
    working_mode.mark_working_mode(
        session["id"],
        mode=MODE,
        meta={
            "project_cwd": project_cwd,
            "file_paths": [],
            "original_contents": {},
            "persistent": persistent,
        },
    )

    full_session = session_manager.get(session["id"])
    return {
        "session_id": session["id"],
        "file_paths": [],
        "original_contents": {},
        "meta_prompt": None,
        "user_ask": _EMPTY_SESSION_ASK,
        "session": full_session,
        "resumed": False,
    }


async def start(
    file_path: str,
    *,
    cwd: str,
    model: Optional[str] = None,
    provider_id: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    persistent: bool = False,
    node_id: str = "primary",
) -> dict:
    """Create (or join) the file-editing session for *cwd* and ensure
    *file_path* is in its set.

    Async because baseline reads (file existence, content) route through
    `node_rpc_handlers.call_local_or_remote` so file_editing works on
    any node the session targets — not just the primary.

    One session per canonical project ``cwd``:
      - No session for the cwd  -> create one, set = {file_path}.
      - Session exists, file already in set -> pure resume.
      - Session exists, file NOT in set -> add it to the set and return
        an add-file meta-prompt (submitted on the SAME claude session,
        continuing the conversation).

    `persistent=True` upgrades the session's persistent flag in place
    (never downgrades).

    Returns: {
      "session_id": str,
      "file_paths": list[str],
      "original_contents": dict[str, str],
      "meta_prompt": str | None,   # None on pure resume
      "session": dict,
      "resumed": bool,
    }
    """
    if not cwd:
        raise ValueError("cwd is required for a file-editing session")

    baseline = await _baseline(node_id, file_path, cwd)
    resolved = baseline["file_path_resolved"]
    project_cwd = baseline["cwd_resolved"]
    orig = baseline["original_content"]

    existing = await asyncio.to_thread(
        working_mode.find_working_session, MODE, project_cwd=project_cwd
    )
    if existing:
        added, sess = await _join_file_set_atomic(
            existing["id"], node_id, resolved, persistent,
        )
        payload = _resume_payload(sess)
        if added:
            payload["meta_prompt"] = _ADD_FILE_PROMPT.format(
                path=resolved,
                file_list=_format_file_list(payload["file_paths"]),
            )
        return payload

    name = f"✏️ Edit — {Path(resolved).name}"
    session = session_manager.create(
        name=name,
        model=model,
        cwd=project_cwd,
        orchestration_mode="native",
        source="web",
        provider_id=provider_id,
        reasoning_effort=reasoning_effort,
        node_id=node_id,
    )
    working_mode.mark_working_mode(
        session["id"],
        mode=MODE,
        meta={
            "project_cwd": project_cwd,
            "file_paths": [resolved],
            "original_contents": {resolved: orig},
            "persistent": persistent,
        },
    )

    full_session = session_manager.get(session["id"])
    return {
        "session_id": session["id"],
        "file_paths": [resolved],
        "original_contents": {resolved: orig},
        "meta_prompt": _META_PROMPT.format(
            file_list=_format_file_list([resolved]),
        ),
        "session": full_session,
        "resumed": False,
    }


def cleanup(session_id: str) -> bool:
    """Delete the file-editor session record. No temp dirs to clean up —
    the real files stay on disk."""
    return working_mode.cleanup_session(session_id)


def is_file_editor_session(session_id: str) -> bool:
    return working_mode.is_working_mode(session_id, MODE)


def _session_or_raise(session_id: str) -> dict:
    session = session_manager.get(session_id)
    if not session or session.get("working_mode") != MODE:
        raise ValueError("not a file-editor session")
    meta = session.get("working_mode_meta") or {}
    _assert_multifile_meta(meta, session_id)
    return session


def _validate_discussion_target(session_id: str, file_path: str, line: int) -> dict:
    session = _session_or_raise(session_id)
    meta = session.get("working_mode_meta") or {}
    file_paths = list(meta.get("file_paths") or [])
    if file_path not in file_paths:
        raise ValueError("file_path is not in this file-editor session")
    if line < 1:
        raise ValueError("line must be >= 1")
    return session


def start_discussion(
    session_id: str,
    *,
    file_path: str,
    line: int,
    title: str = "",
    opened_by: str = "user",
    client_id: Optional[str] = None,
) -> dict:
    _validate_discussion_target(session_id, file_path, line)
    now = datetime.now().isoformat()
    discussion = {
        "id": f"fd_{uuid.uuid4().hex[:12]}",
        "file_path": file_path,
        "line": line,
        "title": title.strip()[:160],
        "collapsed": False,
        "opened_by": opened_by,
        "created_at": now,
        "updated_at": now,
    }
    session = session_manager.upsert_file_discussion(
        session_id, discussion, client_id=client_id,
    )
    if not session:
        raise ValueError("session not found")
    return discussion


def patch_discussion(
    session_id: str,
    discussion_id: str,
    patch: dict,
    *,
    client_id: Optional[str] = None,
) -> dict:
    _session_or_raise(session_id)
    allowed: dict = {}
    if "collapsed" in patch:
        allowed["collapsed"] = bool(patch["collapsed"])
    if "title" in patch:
        allowed["title"] = str(patch.get("title") or "").strip()[:160]
    session = session_manager.patch_file_discussion(
        session_id, discussion_id, allowed, client_id=client_id,
    )
    if not session:
        raise ValueError("session not found")
    meta = session.get("working_mode_meta") or {}
    for discussion in meta.get("file_discussions") or []:
        if discussion.get("id") == discussion_id:
            return discussion
    raise ValueError("discussion not found")


def get_discussion(session_id: str, discussion_id: str) -> dict:
    session = _session_or_raise(session_id)
    meta = session.get("working_mode_meta") or {}
    for discussion in meta.get("file_discussions") or []:
        if discussion.get("id") == discussion_id:
            return discussion
    raise ValueError("discussion not found")


def format_discussion_prompt(discussion: dict, prompt: str) -> str:
    return (
        "<file-discussion>\n"
        f"File: {discussion.get('file_path')}\n"
        f"Line: {discussion.get('line')}\n"
        f"Discussion id: {discussion.get('id')}\n"
        "</file-discussion>\n\n"
        f"{prompt}"
    )
