"""Internal HTTP surface for prompt-engineering sessions.

Prompt refinement runs as its own session, so these routes have to reach
the orchestration layer to submit prompts and tear sessions down. Those
capabilities are injected rather than imported: `coordinator` is
constructed in `main`, and importing back into it would make the cycle
that this extraction exists to remove.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from fastapi import APIRouter, Body, Header, HTTPException

import config_store
import prompt_engineer
from i18n import t
from internal_guards import require_role_internal

router = APIRouter(tags=["prompt-engineer"])

_ROLE = "prompt-engineer"

_submit_prompt: Optional[Callable[[str, dict], Awaitable[Any]]] = None
_cancel_session: Optional[Callable[[str], Awaitable[Any]]] = None
_session_lite: Optional[Callable[[str], Awaitable[Optional[dict]]]] = None
_publish_worker_fanout: Optional[Callable[..., Awaitable[Any]]] = None


def configure(
    submit_prompt: Callable[[str, dict], Awaitable[Any]],
    cancel_session: Callable[[str], Awaitable[Any]],
    session_lite: Callable[[str], Awaitable[Optional[dict]]],
    publish_worker_fanout: Callable[..., Awaitable[Any]],
) -> None:
    global _submit_prompt, _cancel_session, _session_lite, _publish_worker_fanout
    _submit_prompt = submit_prompt
    _cancel_session = cancel_session
    _session_lite = session_lite
    _publish_worker_fanout = publish_worker_fanout


def _required_session_id(body: dict | None) -> str:
    session_id = str((body or {}).get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    return session_id


def _require_eng_session(session_id: str) -> None:
    if not prompt_engineer.is_eng_session(session_id):
        raise HTTPException(status_code=404, detail=t("error.not_eng_session"))


async def _submit(session_id: str, prompt: str, session: dict, client_id: Any) -> None:
    """Sample provider env so the eng-session CLI spawn inherits the right
    ANTHROPIC_API_KEY / BASE_URL / CONFIG_DIR, then submit. Mirrors
    fork_and_send."""
    await asyncio.to_thread(config_store.apply_env_vars)
    await _submit_prompt(session_id, {
        "prompt": prompt,
        "app_session_id": session_id,
        "model": session.get("model"),
        "cwd": session.get("cwd"),
        "ws_callback": None,
        "images": None,
        "orchestration_mode": session.get("orchestration_mode"),
        "client_id": client_id,
    })


@router.post("/api/internal/prompt-engineer/start")
async def internal_prompt_engineering_start(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_role_internal(_ROLE)
    body = body or {}
    session_id = _required_session_id(body)
    # Empty drafts are valid — claude starts the prompt from scratch.
    draft = body.get("draft") or ""
    mode = body.get("mode") or "fork"
    if mode not in ("fork", "new"):
        raise HTTPException(status_code=400, detail=t("error.mode_must_be_fork_or_new"))
    try:
        result = await prompt_engineer.start(session_id, draft, mode)
    except KeyError:
        raise HTTPException(status_code=404, detail=t("error.parent_session_not_found"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    eng_id = result["eng_session_id"]
    eng_session = result["session"] or {}
    # Only fire the meta-prompt on a fresh start. On resume the eng
    # session already had its turn 1 (and possibly many subsequent ones)
    # — re-firing would tell claude "you are refining a prompt" again
    # mid-conversation and corrupt the thread.
    if result.get("meta_prompt") is not None:
        await _submit(eng_id, result["meta_prompt"], eng_session, body.get("client_id"))

    return {
        "eng_session_id": eng_id,
        "temp_file_path": result["temp_file_path"],
        "original_content": result["original_content"],
        "session": eng_session,
        "resumed": bool(result.get("resumed", False)),
    }


@router.post("/api/internal/prompt-engineer/get")
async def internal_prompt_engineering_get(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_role_internal(_ROLE)
    session_id = _required_session_id(body)
    payload = prompt_engineer.get_for_parent(session_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=t("error.no_live_eng_session"))
    return {
        "eng_session_id": payload["eng_session_id"],
        "temp_file_path": payload["temp_file_path"],
        "original_content": payload["original_content"],
        "session": payload["session"],
        "resumed": True,
    }


@router.post("/api/internal/prompt-engineer/comment")
async def internal_prompt_engineering_comment(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_role_internal(_ROLE)
    body = body or {}
    session_id = _required_session_id(body)
    _require_eng_session(session_id)
    try:
        file_path = body["file_path"]
        start_line = int(body["start_line"])
        end_line = int(body["end_line"])
        start_col = int(body["start_col"])
        end_col = int(body["end_col"])
        comment = body["comment"]
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=t("error.missing_invalid_field", e=str(e)))

    message = prompt_engineer.format_comment(
        file_path, start_line, end_line, start_col, end_col, comment,
    )
    eng_session = await _session_lite(session_id) or {}
    await _submit(session_id, message, eng_session, body.get("client_id"))
    return {"submitted": True}


@router.post("/api/internal/prompt-engineer/result")
async def internal_prompt_engineering_result(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_role_internal(_ROLE)
    session_id = _required_session_id(body)
    _require_eng_session(session_id)
    try:
        content = await prompt_engineer.finalize(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=t("error.session_not_found_retry"))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=t("error.temp_file_missing"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    sess = await _session_lite(session_id) or {}
    meta = sess.get("working_mode_meta") or {}
    return {
        "content": content,
        "parent_session_id": meta.get("parent_session_id"),
        "original_content": meta.get("original_content", ""),
    }


@router.post("/api/internal/prompt-engineer/cleanup")
async def internal_prompt_engineering_cleanup(
    body: dict = Body(default={}),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    require_role_internal(_ROLE)
    session_id = _required_session_id(body)
    _require_eng_session(session_id)
    await _cancel_session(session_id)
    ok = await prompt_engineer.cleanup(session_id)
    await _publish_worker_fanout(
        session_id,
        op_label="prompt-eng cleanup",
        caller_scope=True,
        remove_worker=True,
        outer_log_msg="worker fan-out failed during prompt-eng cleanup",
    )
    return {"deleted": ok}
