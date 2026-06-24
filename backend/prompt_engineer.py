"""Prompt-engineering mode — ephemeral Better Agent sessions that iteratively refine
a temp file whose final content becomes the prompt sent to a parent session.

Lifecycle:
  start    -> idempotent: creates an eng session if none exists for the
              parent, otherwise returns the existing one (resume).
  finalize -> read the temp file's current content (becomes the parent prompt).
  cleanup  -> caller cancels the eng session's runners + rearranger first,
              then delegate here to drop the session record + temp dir.
              Idempotent — already-gone sessions are silently no-op'd so
              the parent-delete cascade can't double-fire and crash.

Coordinator + rearranger calls live in main.py — this module is pure
state plumbing so it can stay decoupled from the FastAPI app object.
"""

import logging
from pathlib import Path
from typing import Optional

from event_bus import bus, BusEvent
from i18n import t
from session_manager import manager as session_manager
import config_store
import working_mode
from prompt_templates import render_prompt

logger = logging.getLogger(__name__)

MODE = "prompt_engineering"

_META_PROMPT_REFINE = render_prompt("prompt_engineer/refine.md")

# Used when the user clicked ⚙ Engineer with no typed draft. The file at
# `path` exists but is empty — there is nothing to "refine". Frame the
# task as authoring from scratch so claude doesn't fabricate an
# "original draft" to compare against.
_META_PROMPT_FROM_SCRATCH = render_prompt("prompt_engineer/from_scratch.md")


def _resume_payload(eng: dict) -> dict:
    """Shared response shape for start-resume and explicit GET resume."""
    meta = eng.get("working_mode_meta") or {}
    return {
        "eng_session_id": eng["id"],
        "temp_file_path": meta.get("temp_file_path"),
        "original_content": meta.get("original_content", ""),
        "meta_prompt": None,
        "session": eng,
        "resumed": True,
    }


async def start(parent_session_id: str, draft: str, mode: str) -> dict:
    """Idempotent: returns the live eng session for this parent if one
    already exists; otherwise creates a fresh one and returns it.

    Returns: {
      "eng_session_id": str,
      "temp_file_path": str,
      "original_content": str,
      "meta_prompt": str | None,  # None on resume — caller skips first turn
      "session": dict,
      "resumed": bool,
    }
    """
    if mode not in ("fork", "new"):
        raise ValueError(t("error.mode_must_be_fork_or_new"))
    parent = session_manager.get(parent_session_id)
    if parent is None:
        raise KeyError(parent_session_id)

    # Single-eng-per-parent invariant. Auto-resume an existing one so the
    # user can leave the overlay and come back without losing work; the
    # typed `draft` is intentionally ignored on resume.
    existing = working_mode.find_working_session(
        MODE, parent_session_id=parent_session_id,
    )
    if existing:
        return _resume_payload(existing)

    parent_orchestration = parent.get("orchestration_mode", "team")
    parent_cwd = parent.get("cwd") or ""

    # Validate everything BEFORE creating the eng record so a rejected
    # start doesn't leave a ghost row in the WS broadcaster's wake.
    parent_claude_sid: Optional[str] = None
    if mode == "fork":
        # mode="fork" branches the parent agent session, so the parent's
        # provider must support fork (claude does; gemini-cli 0.42
        # doesn't — issue google-gemini/gemini-cli#22563). Reject HERE
        # so no eng session record is created. mode="fresh" works for
        # every provider.
        parent_provider_id = parent.get("provider_id")
        if parent_provider_id:
            from provider import get_provider as _gp
            try:
                _pinst = _gp(parent_provider_id)
            except KeyError:
                _pinst = None
            if _pinst is not None and not _pinst.supports_fork:
                raise ValueError(
                    f"prompt-engineer refine mode requires fork support; "
                    f"parent's provider ({_pinst.KIND}) does not. Use the "
                    f"'from scratch' mode instead."
                )
        parent_claude_sid = parent.get("agent_session_id")
        if not parent_claude_sid:
            raise ValueError(
                t("prompt_engineer.parent_no_claude_session")
            )

    # The eng session runs on the parent's node — the project context
    # lives there and the temp prompt file must sit where the eng
    # session's CLI can edit it. The dir check and temp-file I/O route
    # through the node RPC layer; for local parents
    # `call_local_or_remote` dispatches in-process (single code path).
    node_id = parent.get("node_id") or "primary"
    from node_rpc_handlers import call_local_or_remote
    if not parent_cwd:
        raise ValueError(
            t("prompt_engineer.parent_cwd_invalid", cwd=repr(parent_cwd))
        )
    chk = await call_local_or_remote(node_id, "dir_exists", {"path": parent_cwd})
    if not chk.get("is_dir"):
        raise ValueError(
            t("prompt_engineer.parent_cwd_invalid", cwd=repr(parent_cwd))
        )

    parent_name = parent.get("name") or t("session.untitled")
    eng = session_manager.create(
        name=(
            t("prompt_engineer.name_fork", parent_name=parent_name) if mode == "fork"
            else t("prompt_engineer.name_fresh")
        ),
        model=parent.get("model") or config_store.default_session_model(),
        cwd=parent_cwd,
        orchestration_mode=parent_orchestration,
        source="web",
        provider_id=parent.get("provider_id"),
        reasoning_effort=parent.get("reasoning_effort"),
        node_id=node_id,
    )

    if mode == "fork":
        assert parent_claude_sid is not None
        session_manager.set_forked_from(eng["id"], parent_claude_sid)

    eng_id = eng["id"]
    written = await call_local_or_remote(
        node_id, "pe_temp_write",
        {"eng_session_id": eng_id, "content": draft},
    )
    tmp_file = Path(written["path"])

    working_mode.mark_working_mode(
        eng_id,
        mode=MODE,
        meta={
            "parent_session_id": parent_session_id,
            "temp_file_path": str(tmp_file),
            "original_content": draft,
            "mode": mode,
        },
    )

    template = _META_PROMPT_REFINE if draft.strip() else _META_PROMPT_FROM_SCRATCH
    meta_prompt = template.format(path=str(tmp_file))

    return {
        "eng_session_id": eng_id,
        "temp_file_path": str(tmp_file),
        "original_content": draft,
        "meta_prompt": meta_prompt,
        "session": session_manager.get(eng_id),
        "resumed": False,
    }


def get_for_parent(parent_session_id: str) -> Optional[dict]:
    """Resume-path lookup: return the bootstrap payload for an existing
    eng session pointed at this parent, or None if there isn't one."""
    existing = working_mode.find_working_session(
        MODE, parent_session_id=parent_session_id,
    )
    if existing is None:
        return None
    return _resume_payload(existing)


def format_comment(
    file_path: str,
    start_line: int,
    end_line: int,
    start_col: int,
    end_col: int,
    comment: str,
) -> str:
    """Render a file-anchored comment. Delegates to shared utility."""
    return working_mode.format_file_comment(
        file_path, start_line, end_line, start_col, end_col, comment,
    )


async def finalize(eng_session_id: str) -> str:
    """Return the current content of the eng session's temp file,
    read from whichever node hosts the eng session."""
    sess = session_manager.get(eng_session_id)
    if sess is None or not working_mode.is_working_mode(eng_session_id, MODE):
        raise KeyError(eng_session_id)
    from node_rpc_handlers import call_local_or_remote
    node_id = sess.get("node_id") or "primary"
    res = await call_local_or_remote(
        node_id, "pe_temp_read", {"eng_session_id": eng_session_id},
    )
    content = res.get("content")
    if content is None:
        raise FileNotFoundError(eng_session_id)
    return content


async def cleanup(eng_session_id: str) -> bool:
    """Delete the eng session record and its temp dir (on whichever
    node hosts it).

    Caller MUST have already awaited coordinator.cancel_session(...)
    AND rearranger.stop(...) BEFORE calling this.
    Idempotent: returns False if already gone. Temp-dir removal is
    best-effort — a dead node must not block deleting the record."""
    import asyncio
    sess = session_manager.get(eng_session_id)
    if sess is None:
        return False
    if not working_mode.is_working_mode(eng_session_id, MODE):
        return False
    from node_rpc_handlers import call_local_or_remote
    node_id = sess.get("node_id") or "primary"
    try:
        await call_local_or_remote(
            node_id, "pe_temp_cleanup", {"eng_session_id": eng_session_id},
        )
    except Exception:
        logger.warning(
            "prompt_engineer: temp-dir cleanup failed for %s on node %s",
            eng_session_id, node_id, exc_info=True,
        )
    return await asyncio.to_thread(
        working_mode.cleanup_session, eng_session_id,
    )


def is_eng_session(session_id: str) -> bool:
    return working_mode.is_working_mode(session_id, MODE)


def register_bus_subscribers() -> None:
    """A15: subscribe to `session.parent_deleted` so the cascade-delete
    handler in main.py doesn't hard-code `if working_mode ==
    "prompt_engineering": prompt_engineer.cleanup()`. The mode-specific
    cleanup now lives next to the mode owner.

    Idempotent — re-binding (e.g. uvicorn --reload) unsubscribes the
    previous registration. Priority 250 (higher than the rearranger
    at 200) — the working_mode catch-all subscribes too and runs at
    260, so this one wins for `prompt_engineering` and the catch-all
    early-exits on the same kind to avoid double-cleanup.

    `cleanup` is async (node-routed temp removal); its sync record
    deletion runs off-loop via `asyncio.to_thread` internally."""

    async def _handler(event: BusEvent) -> None:
        if event.payload.get("working_mode") != MODE:
            return
        child_id = event.payload.get("child_session_id")
        if not child_id:
            return
        try:
            await cleanup(child_id)
        except Exception:
            logger.exception(
                "prompt_engineer subscriber: cleanup failed for %s",
                child_id,
            )

    bus.unsubscribe("prompt_engineer_parent_deleted")
    bus.subscribe(
        "session.parent_deleted",
        _handler,
        priority=250,
        name="prompt_engineer_parent_deleted",
    )
    logger.info(
        "event_bus: registered prompt_engineer parent-deleted subscriber",
    )


def temp_file_path_for(session_id: str) -> Optional[str]:
    sess = session_manager.get(session_id)
    if sess is None:
        return None
    meta = sess.get("working_mode_meta") or {}
    return meta.get("temp_file_path")
