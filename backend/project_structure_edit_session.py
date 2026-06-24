"""Dedicated project-structure review container backed by provisioned forks."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import config_store
import extension_store
import project_update_store
import provisioning
from paths import ba_home, encode_cwd
from provisioning import DirtyPolicy, ProvisionedSessionSpec
from provisioning.config import resolve_config
from provisioning.dispatch import dispatch, extract_fork_text
from provisioning.lifecycle import ensure_session
from provisioning.prompts import render_prompt
from prompt_templates import render_prompt as render_runtime_prompt
import virtual_session_store
import virtual_session_prompt_handlers

logger = logging.getLogger(__name__)

EDIT_EXTENSION_ID = extension_store.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID
EDIT_SINGLETON_ID = f"virtual:{EDIT_EXTENSION_ID}:project-structure-edit"
EDIT_SINGLETON_MODE = "project_structure_edit"
MAINTAINER_WORKER_MODE = "project_structure_maintainer"
MAINTAINER_REVIEW_TIMEOUT_SECONDS = 300
MAINTAINER_REVIEW_STARTED_TEXT = "Project structure maintainer started. Reviewing captured updates..."
_REPO_ROOT = Path(__file__).resolve().parent.parent

# Created lazily inside ensure_singleton to avoid asyncio.Lock outside event loop.
_ensure_lock: Optional[asyncio.Lock] = None
_inflight: Optional[asyncio.Task] = None
_inflight_queued_id: Optional[str] = None


def find_user_message_by_client_id(client_id: Optional[str]) -> Optional[dict]:
    if not client_id:
        return None
    sess = virtual_session_store.get(EDIT_SINGLETON_ID)
    if not sess:
        return None
    return next(
        (
            msg
            for msg in sess.get("messages", [])
            if msg.get("role") == "user" and msg.get("client_id") == client_id
        ),
        None,
    )


def _get_ensure_lock() -> asyncio.Lock:
    global _ensure_lock
    if _ensure_lock is None:
        _ensure_lock = asyncio.Lock()
    return _ensure_lock


def _singleton_cwd(project_cwd: str) -> str:
    p = ba_home() / "project-structure-edit" / encode_cwd(project_cwd)
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _project_meta(project_cwd: str) -> dict:
    return {"project_cwd": str(Path(project_cwd).expanduser().resolve())}


def get_singleton_project_cwd(fallback: str) -> str:
    sess = virtual_session_store.get(EDIT_SINGLETON_ID) or {}
    meta = sess.get("metadata") or {}
    project_cwd = meta.get("project_cwd")
    if isinstance(project_cwd, str) and project_cwd:
        return project_cwd
    return fallback


class ProjectStructureMaintainerSpec(ProvisionedSessionSpec):
    key = MAINTAINER_WORKER_MODE
    version = 1
    name = "project-structure-maintainer"
    env_prefix = "PROJECT_STRUCTURE_MAINTAINER"
    task_key = "project_structure_edit"
    orchestration_mode = "native"
    bare_config = False
    worker_creation_policy = "deny"
    machine_completion = False
    run_mode = "fork"
    ephemeral_forks = False
    dispatch = "in_process"
    on_no_fork = "error"
    default_cwd = str(_REPO_ROOT)
    dirty_policy = DirtyPolicy(
        max_base_bytes=1_000_000,
        max_user_turns=1,
        max_assistant_turns=None,
    )

    def build_provision_prompt(self, ctx: dict) -> str:
        project_cwd = str(ctx.get("project_cwd") or "")
        skill_dir = str(ctx.get("skill_dir") or "")
        return render_prompt(
            "project_structure_maintainer.md",
            {"project_cwd": project_cwd, "skill_dir": skill_dir},
        )

    def build_instructions(self, query: str, ctx: dict) -> str:
        return query


MAINTAINER_SPEC = provisioning.register(ProjectStructureMaintainerSpec())


async def ensure_singleton(project_cwd: str) -> dict:
    """Lazy-create or repurpose the virtual edit singleton for a project."""
    lock = _get_ensure_lock()
    async with lock:
        _edit_llm = config_store.resolve_internal_llm("project_structure_edit")
        return virtual_session_store.upsert(
            EDIT_EXTENSION_ID,
            {
                "id": EDIT_SINGLETON_ID,
                "name": "Project Structure",
                "cwd": _singleton_cwd(project_cwd),
                "model": _edit_llm["model"],
                "provider_id": _edit_llm["provider_id"],
                "node_id": "primary",
                "metadata": {
                    **_project_meta(project_cwd),
                    "working_mode": EDIT_SINGLETON_MODE,
                },
            },
        )


def _find_skill_dir(project_cwd: str) -> Optional[str]:
    """Find the project-structure skill directory for a given project cwd."""
    project = Path(project_cwd)
    for candidate in (
        project / ".agents" / "skills" / "project-structure",
        project / ".claude" / "skills" / "project-structure",
    ):
        if candidate.is_dir():
            return str(candidate)
    return None


def _discover_sections(skill_dir: str) -> list[str]:
    """Read the actual section files from disk instead of hardcoding."""
    sections: list[str] = []
    base = Path(skill_dir) / "sections"
    if not base.is_dir():
        return sections
    for p in sorted(base.rglob("*.md")):
        rel = p.relative_to(Path(skill_dir))
        sections.append(str(rel))
    return sections


def _build_review_prompt(
    updates: list[dict],
    project_cwd: str,
    skill_dir: str,
    sections: list[str],
) -> str:
    """Build the initial review prompt sent to the edit session."""
    updates_text = "\n".join(
        f"- [{u['id']}] {u['text']}" for u in updates
    )
    sections_list = "\n".join(f"- {s}" for s in sections)
    return render_runtime_prompt(
        "project_structure/review.md",
        {
            "project_cwd": project_cwd,
            "skill_dir": skill_dir,
            "sections_list": sections_list,
            "updates_count": len(updates),
            "updates_text": updates_text,
        },
    )


async def submit_review_prompt(project_cwd: str) -> dict:
    """Ensure the singleton exists and submit a review prompt with all
    unseen updates. Returns {"status": "ok"} or {"error": "..."}."""
    project_id = encode_cwd(project_cwd)
    await ensure_singleton(project_cwd)
    async with _get_ensure_lock():
        updates = project_update_store.list_unseen(project_id)
        if not updates:
            return {"error": "no_unseen_updates"}

        running_queued_id = _running_review_queued_id()
        if running_queued_id:
            return {"status": "already_running", "queued_id": running_queued_id}

        skill_dir = _find_skill_dir(project_cwd)
        if not skill_dir:
            return {"error": "skill_dir_not_found"}

        sections = _discover_sections(skill_dir)
        prompt = _build_review_prompt(updates, project_cwd, skill_dir, sections)

        try:
            queued_id = _enqueue_review(
                prompt,
                project_cwd,
                skill_dir,
                update_ids=[str(update["id"]) for update in updates],
            )
            return {"status": "ok", "queued_id": queued_id}
        except Exception:
            logger.exception("submit_review_prompt failed")
            return {"error": "unexpected"}


async def prepare_review_prompt(project_cwd: str) -> dict:
    await ensure_singleton(project_cwd)
    prompt = build_review_prompt(project_cwd)
    if prompt is None:
        return {"status": "no_prompt", "review_prompt": None}
    return {"status": "ok", "review_prompt": prompt}


async def submit_user_prompt(
    project_cwd: str,
    prompt: str,
    *,
    client_id: Optional[str] = None,
    lifecycle_msg_id: Optional[str] = None,
    on_user_message: Optional[Callable[[dict], Awaitable[None]]] = None,
) -> dict:
    await ensure_singleton(project_cwd)
    if find_user_message_by_client_id(client_id):
        return {"error": "duplicate_client_id"}
    skill_dir = _find_skill_dir(project_cwd)
    if not skill_dir:
        return {"error": "skill_dir_not_found"}
    instructions = _build_followup_prompt(prompt, project_cwd, skill_dir)
    try:
        queued_id = _enqueue_review(
            prompt,
            project_cwd,
            skill_dir,
            instructions,
            client_id=client_id,
            lifecycle_msg_id=lifecycle_msg_id,
            on_user_message=on_user_message,
        )
        return {"status": "ok", "queued_id": queued_id}
    except Exception:
        logger.exception("submit_user_prompt failed")
        return {"error": "unexpected"}


async def handle_virtual_prompt(
    session_id: str,
    prompt: str,
    cwd: str,
    client_id: Optional[str],
    lifecycle_msg_id: Optional[str],
    dispatch_ws: virtual_session_prompt_handlers.DispatchWS,
) -> bool:
    if session_id != EDIT_SINGLETON_ID:
        return False
    not_ready = extension_store.runtime_not_ready_message(EDIT_EXTENSION_ID)
    if not_ready is not None:
        await _dispatch_prompt_error(
            dispatch_ws,
            not_ready,
            session_id=session_id,
            client_id=client_id,
        )
        return True
    already_done = find_user_message_by_client_id(client_id)
    if already_done:
        await dispatch_ws({
            "type": "user_message_persisted",
            "data": {
                "session_id": session_id,
                "user_message": already_done,
            },
        })
        return True

    async def _ack_user_message(user_message: dict) -> None:
        await dispatch_ws({
            "type": "user_message_persisted",
            "data": {
                "session_id": session_id,
                "user_message": user_message,
            },
        })

    result = await submit_user_prompt(
        get_singleton_project_cwd(cwd),
        prompt,
        client_id=client_id,
        lifecycle_msg_id=lifecycle_msg_id,
        on_user_message=_ack_user_message,
    )
    error = result.get("error")
    if error:
        await _dispatch_prompt_error(
            dispatch_ws,
            str(error),
            session_id=session_id,
            client_id=client_id,
        )
    return True


async def _dispatch_prompt_error(
    dispatch_ws: virtual_session_prompt_handlers.DispatchWS,
    error: str,
    *,
    session_id: str,
    client_id: Optional[str],
) -> None:
    await dispatch_ws({
        "type": "error",
        "data": {
            "error": error,
            "app_session_id": session_id,
            "session_id": session_id,
            "client_id": client_id,
        },
    })


def _enqueue_review(
    prompt: str,
    project_cwd: str,
    skill_dir: str,
    instructions: Optional[str] = None,
    *,
    update_ids: Optional[list[str]] = None,
    client_id: Optional[str] = None,
    lifecycle_msg_id: Optional[str] = None,
    on_user_message: Optional[Callable[[dict], Awaitable[None]]] = None,
) -> str:
    global _inflight, _inflight_queued_id
    queued_id = uuid.uuid4().hex
    _inflight = asyncio.create_task(
        _run_review(
            queued_id,
            prompt,
            project_cwd,
            skill_dir,
            instructions or prompt,
            update_ids=update_ids,
            client_id=client_id,
            lifecycle_msg_id=lifecycle_msg_id,
            on_user_message=on_user_message,
        ),
        name="project_structure_review",
    )
    _inflight_queued_id = queued_id
    _inflight.add_done_callback(_clear_inflight_if_current(queued_id))
    return queued_id


def _running_review_queued_id() -> Optional[str]:
    if _inflight is None or _inflight.done():
        return None
    return _inflight_queued_id


def _clear_inflight_if_current(queued_id: str) -> Callable[[asyncio.Task], None]:
    def _clear(_task: asyncio.Task) -> None:
        global _inflight, _inflight_queued_id
        if _inflight_queued_id != queued_id:
            return
        _inflight = None
        _inflight_queued_id = None

    return _clear


async def _run_review(
    queued_id: str,
    prompt: str,
    project_cwd: str,
    skill_dir: str,
    instructions: str,
    *,
    update_ids: Optional[list[str]],
    client_id: Optional[str],
    lifecycle_msg_id: Optional[str],
    on_user_message: Optional[Callable[[dict], Awaitable[None]]],
) -> None:
    from main import coordinator as _coordinator

    await _append_visible_turn(
        prompt,
        queued_id,
        is_streaming=True,
        initial_assistant_content=MAINTAINER_REVIEW_STARTED_TEXT,
        client_id=client_id,
        lifecycle_msg_id=lifecycle_msg_id,
        on_user_message=on_user_message,
    )
    msg_id = queued_id
    try:
        cfg = resolve_config(MAINTAINER_SPEC)
        cfg = replace(
            cfg,
            cwd=project_cwd,
            caller_session_id=EDIT_SINGLETON_ID,
        )
        ctx = {"project_cwd": project_cwd, "skill_dir": skill_dir}
        base_session_id = ensure_session(MAINTAINER_SPEC, cfg)
        result = await asyncio.wait_for(
            dispatch(
                MAINTAINER_SPEC,
                cfg,
                base_session_id=base_session_id,
                caller_session_id=EDIT_SINGLETON_ID,
                instructions=MAINTAINER_SPEC.build_instructions(instructions, ctx),
                provision_prompt=MAINTAINER_SPEC.build_provision_prompt(ctx),
            ),
            timeout=MAINTAINER_REVIEW_TIMEOUT_SECONDS,
        )
        if not result.get("success"):
            raise RuntimeError(str(result.get("error") or "maintainer fork failed"))
        text = extract_fork_text(result) or "(no response)"
        _update_assistant_message(msg_id, content=text, is_streaming=False)
        if update_ids:
            project_id = encode_cwd(project_cwd)
            project_update_store.mark_seen(project_id, update_ids)
            await _coordinator.broadcast_global(
                "project_updates_changed",
                {
                    "project_id": project_id,
                    "unseen_count": project_update_store.unseen_count(project_id),
                },
            )
        await _dispatch_message_delta(_coordinator, msg_id)
    except asyncio.TimeoutError:
        logger.exception("project-structure provisioned review timed out")
        _update_assistant_message(
            msg_id,
            content=(
                "Project structure maintainer timed out before returning a result. "
                "Captured updates were left pending."
            ),
            is_streaming=False,
        )
        await _dispatch_message_delta(_coordinator, msg_id)
    except Exception as exc:
        logger.exception("project-structure provisioned review failed")
        _update_assistant_message(msg_id, content=str(exc), is_streaming=False)
        await _dispatch_message_delta(_coordinator, msg_id)


def _build_followup_prompt(prompt: str, project_cwd: str, skill_dir: str) -> str:
    return render_runtime_prompt(
        "project_structure/followup.md",
        {
            "project_cwd": project_cwd,
            "skill_dir": skill_dir,
            "prompt": prompt,
        },
    )


async def _append_visible_turn(
    prompt: str,
    assistant_msg_id: str,
    *,
    is_streaming: bool,
    initial_assistant_content: str = "",
    client_id: Optional[str] = None,
    lifecycle_msg_id: Optional[str] = None,
    on_user_message: Optional[Callable[[dict], Awaitable[None]]] = None,
) -> None:
    from main import coordinator as _coordinator

    user_msg = {
        "id": uuid.uuid4().hex,
        "role": "user",
        "content": prompt,
        "timestamp": datetime.now().isoformat(),
        "client_id": client_id,
        "lifecycle_msg_id": lifecycle_msg_id,
    }
    assistant_msg = {
        "id": assistant_msg_id,
        "role": "assistant",
        "content": initial_assistant_content,
        "events": [],
        "timestamp": datetime.now().isoformat(),
        "isStreaming": is_streaming,
    }
    session = virtual_session_store.get(EDIT_SINGLETON_ID)
    messages = list((session or {}).get("messages") or [])
    messages.extend([user_msg, assistant_msg])
    virtual_session_store.replace_messages(EDIT_EXTENSION_ID, EDIT_SINGLETON_ID, messages)
    persisted = virtual_session_store.get(EDIT_SINGLETON_ID) or {}
    persisted_messages = persisted.get("messages") or []
    persisted_user_msg = next(
        (
            msg for msg in persisted_messages
            if isinstance(msg, dict) and msg.get("id") == user_msg["id"]
        ),
        user_msg,
    )
    persisted_assistant_msg = next(
        (
            msg for msg in persisted_messages
            if isinstance(msg, dict) and msg.get("id") == assistant_msg["id"]
        ),
        assistant_msg,
    )
    await _coordinator._dispatch_messages_delta(
        EDIT_SINGLETON_ID, EDIT_SINGLETON_ID, persisted_user_msg,
    )
    if on_user_message is not None:
        await on_user_message(persisted_user_msg)
    await _coordinator._dispatch_messages_delta(
        EDIT_SINGLETON_ID, EDIT_SINGLETON_ID, persisted_assistant_msg,
    )


async def _dispatch_message_delta(coordinator: Any, msg_id: str) -> None:
    sess = virtual_session_store.get(EDIT_SINGLETON_ID) or {}
    msg = next(
        (
            m for m in sess.get("messages", [])
            if isinstance(m, dict) and m.get("id") == msg_id
        ),
        None,
    )
    if msg is not None:
        await coordinator._dispatch_messages_delta(
            EDIT_SINGLETON_ID, EDIT_SINGLETON_ID, msg,
        )


def _update_assistant_message(
    msg_id: str,
    *,
    content: Optional[str] = None,
    is_streaming: Optional[bool] = None,
) -> None:
    session = virtual_session_store.get(EDIT_SINGLETON_ID)
    if not session:
        return
    messages = list(session.get("messages") or [])
    changed = False
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("id") != msg_id:
            continue
        if content is not None:
            msg["content"] = content
            changed = True
        if is_streaming is not None:
            msg["isStreaming"] = is_streaming
            changed = True
        break
    if changed:
        virtual_session_store.replace_messages(
            EDIT_EXTENSION_ID,
            EDIT_SINGLETON_ID,
            messages,
        )


def build_review_prompt(project_cwd: str) -> Optional[str]:
    """Build the review prompt from unseen updates. Returns None if no updates."""
    project_id = encode_cwd(project_cwd)
    updates = project_update_store.list_unseen(project_id)
    if not updates:
        return None

    skill_dir = _find_skill_dir(project_cwd)
    if not skill_dir:
        return None

    sections = _discover_sections(skill_dir)
    return _build_review_prompt(updates, project_cwd, skill_dir, sections)


def get_edit_status(project_cwd: str) -> dict:
    """Return the current edit status for a project."""
    project_id = encode_cwd(project_cwd)
    return {
        "project_id": project_id,
        "unseen_count": project_update_store.unseen_count(project_id),
        "unseen_updates": project_update_store.list_unseen(project_id),
    }


virtual_session_prompt_handlers.register(EDIT_SINGLETON_ID, handle_virtual_prompt)
