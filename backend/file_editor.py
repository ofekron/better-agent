"""File-editing mode — Better Agent sessions that interactively edit real
project files with AI assistance.

Lifecycle:
  start/start_empty -> warm one generic file-editing base for the project
                       cwd/provider/model, then create a fresh user-facing
                       provider fork for this new editor session. Sessions
                       are no longer reused by cwd or by file path.
  cleanup           -> caller cancels runners first, then
                       delegates here to drop the session record.

Unlike prompt engineering, file-editor sessions edit the REAL files in-place.
The generic provisioned base learns the editing workflow once (without any
specific file); every user-facing file-editor session forks from that warm
base and receives only its own selected file set / user prompt.

`persistent` is now a plain per-session creation flag: persistent sessions are
sidebar-visible; temporal sessions are sidebar-hidden and show the Done button.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional
import uuid
from datetime import datetime

from session_manager import manager as session_manager
import config_store
import session_readiness
import working_mode
from prompt_templates import render_prompt
from provisioning import DirtyPolicy, ProvisionedConfig, ProvisionedSessionSpec, register
from reasoning_effort import normalize_reasoning_effort
import provisioning.manager as provisioning_manager
from hot_path_executor import hot_path

logger = logging.getLogger(__name__)

MODE = "file_editing"
BASE_MODE = "file_editing_base"

# In-flight background warms, keyed by the session awaiting its binding.
# Prevents a retried create or a post-failure re-arm from running two
# warms against the same session.
_PROVISIONING_TASKS: dict[str, "asyncio.Task"] = {}

_META_PROMPT = render_prompt("file_editor/bootstrap.md")
_PROVISION_PROMPT = render_prompt("file_editor/provision.md")

_EMPTY_SESSION_ASK = (
    "Which file or files do you want to edit? You can pick files with the "
    "file chooser, ask me to create a new file, or describe the files here."
)


class FileEditBaseSpec(ProvisionedSessionSpec):
    """Warm base used only as the provider-level fork source for file editing."""

    key = BASE_MODE
    version = 1
    name = "file-editing-base"
    env_prefix = "FILE_EDITING_BASE"
    task_key = "default_session"
    orchestration_mode = "native"
    bare_config = False
    worker_creation_policy = "deny"
    machine_completion = False
    run_mode = "fork"
    ephemeral_forks = True
    dispatch = "in_process"
    on_no_fork = "error"
    lifetime_seconds = 6 * 60 * 60
    provision_timeout = 24.0 * 60.0 * 60.0
    retry_attempts = 1
    dirty_policy = DirtyPolicy(
        max_base_bytes=1_000_000,
        max_user_turns=1,
        max_assistant_turns=1,
    )

    def build_provision_prompt(self, ctx: dict) -> str:
        return _PROVISION_PROMPT


FILE_EDIT_BASE_SPEC = register(FileEditBaseSpec())


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


def _provider_record(provider_id: Optional[str]) -> dict:
    resolved_provider_id = provider_id or config_store.default_session_provider_id()
    provider = (
        config_store.get_provider(resolved_provider_id)
        if resolved_provider_id else
        config_store.get_default_provider()
    )
    if resolved_provider_id and not provider:
        raise ValueError("provider not found")
    if not provider:
        raise ValueError("no active provider configured")
    return provider


def _require_fork_support(provider_id: str, runner: str) -> None:
    try:
        from provider import get_provider
        provider = get_provider(provider_id, runner)
    except KeyError as exc:
        raise ValueError("provider not found") from exc
    if not getattr(provider, "supports_fork", True):
        raise ValueError(
            f"file-editing sessions require fork support; "
            f"provider {getattr(provider, 'KIND', provider_id)!r} does not support fork."
        )


def _file_edit_config(
    *,
    project_cwd: str,
    model: Optional[str],
    provider_id: Optional[str],
    runner: Optional[str],
    reasoning_effort: Optional[str],
    node_id: str,
) -> ProvisionedConfig:
    provider = _provider_record(provider_id)
    resolved_provider_id = str(provider.get("id") or provider_id or "")
    if not resolved_provider_id:
        raise ValueError("provider not found")
    from runtime_profile import resolve_runner

    profile_defaults = config_store.provider_execution_defaults(
        resolved_provider_id, runner
    )
    resolved_runner = resolve_runner(
        provider, runner or profile_defaults["runner"]
    )
    _require_fork_support(resolved_provider_id, resolved_runner)
    resolved_model = str(model or profile_defaults["default_model"] or "").strip()
    if not resolved_model:
        name = provider.get("name") or resolved_provider_id
        raise ValueError(f"{name} has no default model configured")
    resolved_effort = normalize_reasoning_effort(reasoning_effort) or ""
    if not resolved_effort and provider.get("supports_reasoning_effort"):
        options = provider.get("reasoning_effort_options") or []
        default_effort = normalize_reasoning_effort(
            profile_defaults["default_reasoning_effort"]
        )
        if default_effort and default_effort in options:
            resolved_effort = default_effort
    return ProvisionedConfig(
        cwd=project_cwd,
        model=resolved_model,
        provider_id=resolved_provider_id,
        reasoning_effort=resolved_effort,
        runner=resolved_runner,
        run_mode="fork",
        dispatch="in_process",
        on_no_fork="error",
        node_id=node_id or "primary",
        backend_url="http://localhost:8000",
        internal_token="",
        provisioned_session_id=None,
        caller_session_id=None,
        worker_description=FILE_EDIT_BASE_SPEC.name,
    )


async def _ensure_file_edit_base(cfg: ProvisionedConfig) -> str:
    """Return a warmed generic file-editing base session id.

    Kept as a small wrapper so deterministic tests can monkeypatch it without
    launching a real provider subprocess.
    """
    return await provisioning_manager.ensure_warm_base(
        FILE_EDIT_BASE_SPEC,
        cfg,
        {},
    )


def _base_line_count(project_cwd: str, base_session: dict, base_agent_sid: str) -> int:
    """Line-count snapshot for the warm base jsonl.

    Stamped on the child so its first provider fork can skip inherited
    provision lines before native tailing exposes the session to the UI.
    """
    try:
        from orchs.jsonl_helpers import compute_jsonl_read_path, count_jsonl_lines
        path = compute_jsonl_read_path(project_cwd, base_agent_sid, base_session)
        return count_jsonl_lines(path) if path else 0
    except Exception:
        logger.debug("file-edit base line-count failed", exc_info=True)
        return 0


async def _base_line_count_async(
    project_cwd: str,
    base_session: dict,
    base_agent_sid: str,
) -> int:
    return await hot_path.run(
        "file_editor.base_line_count",
        _base_line_count,
        project_cwd,
        base_session,
        base_agent_sid,
    )


async def _create_interactive_fork(
    *,
    project_cwd: str,
    file_paths: list[str],
    original_contents: dict[str, str],
    persistent: bool,
    name: str,
    cfg: ProvisionedConfig,
    session_id: Optional[str] = None,
) -> tuple[dict, bool]:
    """Returns `(session, resumed)`. `resumed=True` means a concurrent create
    carrying the same `session_id` won the race — the winner owns session
    setup and bootstrap injection; the caller must not repeat either.

    Returns WITHOUT waiting for the provisioned base. Warming it spawns a
    provider CLI and runs a one-time provision turn (20-30s healthy, and
    serialized per base identity), which is far too long to hold an HTTP
    request open. Everything knowable now is bound now; the three
    warm-dependent fields are bound by `_provision_fork` in the
    background, and until then `session_readiness` holds the session's
    prompts at the dispatch gate.
    """
    try:
        session = session_manager.create(
            name=name,
            model=cfg.model,
            cwd=project_cwd,
            orchestration_mode="native",
            source="web",
            provider_id=cfg.provider_id,
            runner=cfg.runner,
            reasoning_effort=cfg.reasoning_effort or None,
            node_id=cfg.node_id,
            # File-editing sessions are opened by an explicit user action.
            user_initiated=True,
            # Client-supplied idempotency key: a timed-out create retried by
            # the frontend resolves to this same session, never a duplicate.
            id=session_id,
            # Written as part of the FIRST record write, so a crash before
            # the next persist cannot bring the session back dispatchable
            # but unbound.
            fork_provisioning=session_readiness.pending_fact(),
        )
    except ValueError:
        existing = session_manager.get(session_id) if session_id else None
        if existing is None:
            raise
        return existing, True
    sid = session["id"]
    working_mode.mark_working_mode(
        sid,
        mode=MODE,
        meta={
            "project_cwd": project_cwd,
            "file_paths": list(file_paths),
            "original_contents": dict(original_contents),
            "persistent": persistent,
            # base_session_id is stamped by the background warm.
        },
    )
    # The session was just created as a normal user session, then marked as
    # working-mode. Force the debounced summary write through before returning
    # so a simultaneous `/api/sessions` snapshot doesn't briefly surface a
    # temporal file-editor session in the sidebar without its working_mode meta.
    await asyncio.to_thread(session_manager.flush_pending_persists)
    schedule_fork_provisioning(sid, cfg, project_cwd)
    return session_manager.get(sid) or session, False


def _commit_fork_binding(
    sid: str,
    base_session_id: str,
    base_agent_sid: str,
    parent_lines: int,
) -> None:
    """Bind the three warm-dependent fields. Runs off the event loop."""
    current = session_manager.get_fields(sid, ("agent_session_id",)) or {}
    if current.get("agent_session_id"):
        # `is_fork_first_turn` requires the session to have NO provider sid
        # of its own, so a fork marker stamped now could never be consumed
        # and would sit on the record forever. Refuse loudly instead.
        raise RuntimeError(
            "session already has its own provider session; refusing to stamp "
            "a fork marker that can never be consumed"
        )
    session_manager.set_forked_from(sid, base_agent_sid)
    if parent_lines > 0:
        session_manager._run(
            sid,
            lambda s: s.__setitem__("parent_line_count_at_fork", parent_lines),
            {"kind": "fork_parent_line_count_set"},
            bump_updated_at=False,
        )
    working_mode.update_working_mode_meta(sid, {"base_session_id": base_session_id})
    session_manager.flush_pending_persists()


async def _provision_fork(
    sid: str,
    cfg: ProvisionedConfig,
    project_cwd: str,
) -> None:
    """Warm the base and bind the fork, then release the session's queued
    prompts. Any failure is recorded on the session so the waiting prompts
    fail visibly rather than hanging."""
    try:
        base_session_id = await asyncio.wait_for(
            _ensure_file_edit_base(cfg),
            timeout=session_readiness.WARM_TIMEOUT_S,
        )
        base_session = session_manager.get(base_session_id) or {}
        base_agent_sid = str(base_session.get("agent_session_id") or "").strip()
        if not base_agent_sid:
            raise RuntimeError("file-editing base did not initialize")
        parent_lines = await _base_line_count_async(
            project_cwd,
            base_session,
            base_agent_sid,
        )
        await asyncio.to_thread(
            _commit_fork_binding,
            sid,
            base_session_id,
            base_agent_sid,
            parent_lines,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "file-edit provisioning failed for %s: %s", sid[:8], exc, exc_info=True,
        )
        await session_readiness.mark_failed(sid, exc)
        return
    # Only after the binding is persisted — waking waiters first would let
    # a turn dispatch before `forked_from_agent_sid` is readable.
    await session_readiness.mark_ready(sid)


def schedule_fork_provisioning(
    sid: str,
    cfg: ProvisionedConfig,
    project_cwd: str,
) -> None:
    """Start the background warm for `sid`, unless one is already running.

    Deduped so an idempotent create (a retry resolving to the same
    session) or a re-arm after failure never runs two warms against one
    session.
    """
    existing = _PROVISIONING_TASKS.get(sid)
    if existing is not None and not existing.done():
        return
    task = asyncio.create_task(
        _provision_fork(sid, cfg, project_cwd),
        name=f"file-edit-provision:{sid[:8]}",
    )
    _PROVISIONING_TASKS[sid] = task
    task.add_done_callback(lambda t: _PROVISIONING_TASKS.pop(sid, None))


async def start_empty(
    *,
    cwd: str,
    model: Optional[str] = None,
    provider_id: Optional[str] = None,
    runner: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    persistent: bool = False,
    node_id: str = "primary",
    session_id: Optional[str] = None,
) -> dict:
    if not cwd:
        raise ValueError("cwd is required for a file-editing session")

    project = await _project_cwd(node_id, cwd)
    project_cwd = project["cwd_resolved"]
    cfg = _file_edit_config(
        project_cwd=project_cwd,
        model=model,
        provider_id=provider_id,
        runner=runner,
        reasoning_effort=reasoning_effort,
        node_id=node_id,
    )

    full_session, resumed = await _create_interactive_fork(
        project_cwd=project_cwd,
        file_paths=[],
        original_contents={},
        persistent=persistent,
        name=f"✏️ Edit — {Path(project_cwd).name}",
        cfg=cfg,
        session_id=session_id,
    )
    return {
        "session_id": full_session["id"],
        "file_paths": [],
        "original_contents": {},
        "meta_prompt": None,
        "user_ask": None if resumed else _EMPTY_SESSION_ASK,
        "session": full_session,
        "resumed": resumed,
    }


async def start(
    file_path: str,
    *,
    cwd: str,
    model: Optional[str] = None,
    provider_id: Optional[str] = None,
    runner: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    persistent: bool = False,
    node_id: str = "primary",
    session_id: Optional[str] = None,
) -> dict:
    """Create a fresh file-editing session for *file_path*.

    Async because baseline reads (file existence, content) route through
    `node_rpc_handlers.call_local_or_remote` so file_editing works on
    any node the session targets — not just the primary.

    Every call creates a new user-facing Better Agent session backed by a
    provider fork of the warmed file-editing base. There is intentionally no
    cwd/file-path reuse or join path. The one exception is `session_id`,
    the client's idempotency key: a concurrent/retried create carrying the
    same id resolves to that session with `resumed=True` and no bootstrap
    prompt, instead of minting a duplicate.

    Returns: {
      "session_id": str,
      "file_paths": list[str],
      "original_contents": dict[str, str],
      "meta_prompt": str | None,
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
    cfg = _file_edit_config(
        project_cwd=project_cwd,
        model=model,
        provider_id=provider_id,
        runner=runner,
        reasoning_effort=reasoning_effort,
        node_id=node_id,
    )

    full_session, resumed = await _create_interactive_fork(
        project_cwd=project_cwd,
        file_paths=[resolved],
        original_contents={resolved: orig},
        persistent=persistent,
        name=f"✏️ Edit — {Path(resolved).name}",
        cfg=cfg,
        session_id=session_id,
    )
    return {
        "session_id": full_session["id"],
        "file_paths": [resolved],
        "original_contents": {resolved: orig},
        "meta_prompt": None if resumed else _META_PROMPT.format(
            file_list=_format_file_list([resolved]),
        ),
        "session": full_session,
        "resumed": resumed,
    }

def _config_for_session(session: dict) -> tuple[ProvisionedConfig, str]:
    meta = session.get("working_mode_meta") or {}
    project_cwd = str(meta.get("project_cwd") or session.get("cwd") or "")
    if not project_cwd:
        raise RuntimeError("session has no project cwd to provision against")
    cfg = _file_edit_config(
        project_cwd=project_cwd,
        model=session.get("model"),
        provider_id=session.get("provider_id"),
        runner=session.get("runner"),
        reasoning_effort=session.get("reasoning_effort"),
        node_id=session.get("node_id") or "primary",
    )
    return cfg, project_cwd


async def rearm_fork_provisioning_if_failed(sid: str) -> bool:
    """Give a session whose provisioning failed another attempt.

    Without this, `failed` is terminal: every later prompt on the session
    is rejected forever with no way back. Re-arming when the user sends a
    new prompt makes "try again" the natural recovery, and the dedup in
    `schedule_fork_provisioning` keeps a retry storm to one live warm.
    """
    if not session_readiness.fork_provisioning_failed(sid):
        return False
    session = await asyncio.to_thread(session_manager.get, sid)
    if not session or session.get("working_mode") != MODE:
        return False
    try:
        cfg, project_cwd = _config_for_session(session)
    except Exception as exc:
        logger.warning("cannot re-arm provisioning for %s: %s", sid[:8], exc)
        return False
    await session_readiness.mark_pending(sid)
    schedule_fork_provisioning(sid, cfg, project_cwd)
    return True


async def recover_pending_fork_provisioning() -> dict:
    """Re-arm background warms interrupted by a backend restart.

    A session persisted while its warm was in flight comes back with the
    pending fact but no live task, and nothing else would ever set its
    resolution event — every prompt on it would wait at the dispatch gate
    forever. Crash recovery re-enqueues durable queued prompts, so this
    MUST run before prompt admission reopens.

    A pending session that cannot be re-armed is failed rather than left
    pending: a visible failure is recoverable, silent parking is not.
    """
    rearmed: list[str] = []
    failed: list[str] = []
    for session in list(session_manager.iter_root_sessions()):
        if session_readiness.state_of(session) != session_readiness.STATE_PENDING:
            continue
        sid = session.get("id")
        if not sid:
            continue
        try:
            cfg, project_cwd = _config_for_session(session)
            schedule_fork_provisioning(sid, cfg, project_cwd)
        except Exception as exc:
            logger.warning(
                "cannot re-arm file-edit provisioning for %s: %s", sid[:8], exc,
            )
            await session_readiness.mark_failed(sid, exc)
            failed.append(sid)
            continue
        rearmed.append(sid)
    if rearmed or failed:
        logger.info(
            "file-edit provisioning recovery: re-armed=%d failed=%d",
            len(rearmed),
            len(failed),
        )
    return {"rearmed": rearmed, "failed": failed}


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
