"""Session search and Ask picker state.

Two shared utilities back every search surface:

  - `run_search_sessions_session(query, *, propose=..., propose_target=,
     propose_msg_id=, timeout=, max_results=)` — the ranking engine. Runs
     on a provisioned session: one hidden base session is primed once (its
     provision prompt loads the `search-in-sessions` skill + the JSON-answer
     contract), then **forked** per call so each fork only carries the real
     query and greps the session transcripts. The base persists across
     searches; the per-call forks are ephemeral. When `propose` is set it
     also stamps the picker on the target in the same call (a "batch"
     search+propose).

  - `propose_sessions(list, reasoning, *, target_sid, msg_id)` — stamps the
     session picker (`ask_result`) on a target session's assistant message
     and broadcasts `message_ask_result_changed`.

Consumers:
  - **Ask flow** (`search()`): appends a user turn on the stable Ask
    session, runs a worker, appends an assistant turn carrying the
    reasoning + picker. The Ask session runs NO claude turns itself — it is
    a UI container; the only LLM is the ephemeral worker.
  - **session-bridge `search_sessions` MCP tool**: wraps
    `run_search_sessions_session` (agent's `propose` flag → batch vs split).
  - **`propose_sessions` MCP tool**: wraps the stamp utility (target =
    caller).

State-ownership: each ask turn's assistant message carries its own
`ask_result` (the picker payload) and `chosen_session_id` (the user's pick),
pushed per-message via `message_ask_result_changed` /
`message_ask_choice_changed` and rehydrated from the persisted message.
Pure UI-driving msg metadata, outside `msg.events` and the convergence
invariant (same class as `retrying_until`).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import perf
import project_store
import config_store
import provisioning
import session_store
import virtual_session_store
import working_mode
from event_shape import extract_output_text, strip_synthetic_events
from paths import ba_home
from provisioning import DirtyPolicy, ProvisionedSessionSpec
from provisioning.prompts import render_prompt
from session_manager import manager as session_manager

logger = logging.getLogger(__name__)


ASK_EXTENSION_ID = "ofek-dev.ask"

# Stable virtual identity for the Ask UI container. Frontend shares the same
# constant in `askSession.ts`.
ASK_SINGLETON_ID = f"virtual:{ASK_EXTENSION_ID}:ask"

# Working-mode tags. `working_mode.should_hide_from_sidebar` returns True
# for any unknown mode, so these also hide the sessions from `_build_index`.
ASK_SINGLETON_MODE = "ask_singleton"      # the stable Ask UI container
SEARCH_WORKER_MODE = "search_worker"      # an ephemeral ranking worker


# Truncate every indexed user-prompt to this length.
_USER_PROMPT_TRUNCATE = 200

# Default ceiling on returned ids. Caller can override per-call.
_DEFAULT_MAX_RESULTS = 20

# Default per-call timeout. A full claude turn takes longer than a one-shot
# headless, and the grep worker must scan many transcripts — give it the
# full 15 min. This is the binding budget (the asyncio.wait_for around
# provisioning.run); private session-bridge MCP transport timeouts must
# exceed it so they never preempt the search.
_DEFAULT_TIMEOUT_SECONDS = 15 * 60


# ── Index building ─────────────────────────────────────────────────────


def _extract_first_user_prompt(messages: list) -> str:
    """Find the first user message and return its text content truncated
    to `_USER_PROMPT_TRUNCATE`. Content may be a plain string or a list
    of typed blocks (claude's content-block shape); both are flattened
    into a single string. Returns empty string when no user message
    exists.
    """
    if not isinstance(messages, list):
        return ""
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            text = "\n".join(parts)
        else:
            text = ""
        text = text.strip()
        if not text:
            continue
        if len(text) > _USER_PROMPT_TRUNCATE:
            return text[:_USER_PROMPT_TRUNCATE] + "…"
        return text
    return ""


def _project_name(cwd: str) -> str:
    """Last path segment, mirrors the frontend `projectName(cwd)` util.
    Empty string when cwd is missing — matches what the index user sees
    in the UI label.
    """
    if not cwd:
        return ""
    try:
        return Path(cwd).name or cwd
    except (TypeError, ValueError):
        return cwd


def _build_index() -> list[dict]:
    session_store._ensure_summary_index(blocking=True)
    index: list[dict] = []
    for data in session_store.list_sessions():
        sid = data.get("id") if isinstance(data, dict) else None
        if not sid:
            continue
        if sid == ASK_SINGLETON_ID:
            continue
        if working_mode.should_hide_from_sidebar(data):
            continue
        if data.get("archived"):
            continue
        cwd = data.get("cwd", "") or ""
        index.append({
            "id": sid,
            "name": data.get("name") or "(untitled)",
            "cwd": cwd,
            "project_name": _project_name(cwd),
            "first_user_prompt": data.get("first_prompt") or "",
            "updated_at": data.get("updated_at", ""),
            "message_count": int(data.get("message_count") or 0),
        })
    # Most-recently-updated first — gives the model a useful prior when
    # multiple sessions look similar.
    index.sort(
        key=lambda s: session_store.timestamp_sort_value(s.get("updated_at")),
        reverse=True,
    )
    return index


def index_stub_map() -> dict[str, dict]:
    """`{id: stub}` for every listable session. Used to enrich the ids a
    search returns with metadata, and to reject ids that aren't
    real/listable (hidden, ephemeral, singleton) — fail closed."""
    return {s["id"]: s for s in _build_index()}


def validate_proposed(session_ids: list) -> list[str]:
    """Keep only ids that resolve to a real, listable session (drops the
    Ask container itself, hidden/ephemeral workers, and unknown ids)."""
    if not isinstance(session_ids, list):
        return []
    valid_ids = {s["id"] for s in _build_index()}
    out: list[str] = []
    seen: set[str] = set()
    for sid in session_ids:
        if (
            isinstance(sid, str)
            and sid in valid_ids
            and sid not in seen
        ):
            seen.add(sid)
            out.append(sid)
    return out


def _resolve_proposed_project(path: str) -> tuple[str, str]:
    """Validate the model's `proposed_project_path` against `project_store`
    AND return the matching project's `node_id`. Returns `("", "")` when
    the path doesn't match a known project — defends against hostile-prompt
    injections (a search query containing path strings could otherwise
    steer the model into pre-filling the NewSessionModal with an arbitrary
    cwd like `~/.ssh`).

    Multi-machine: when two projects on different nodes share the same
    `path`, picks the most-recently-used one. Swallows project_store load
    errors: the model's hint is optional, so a projects.json hiccup must
    NOT 500 the turn."""
    if not isinstance(path, str) or not path:
        return ("", "")
    try:
        projects = project_store.list_projects()
    except Exception:
        logger.warning(
            "_resolve_proposed_project: list_projects failed; dropping hint",
            exc_info=True,
        )
        return ("", "")
    match = next((p for p in projects if p.get("path") == path), None)
    if match is None:
        return ("", "")
    return (path, match.get("node_id") or "primary")


# ── Propose (picker stamp) utility ──────────────────────────────────────


def propose_sessions(
    session_ids: list,
    reasoning: str,
    *,
    target_sid: str,
    msg_id: str,
    proposed_project_path: str = "",
) -> dict:
    """Validate the ranked ids and stamp the session picker (`ask_result`)
    on assistant message `msg_id` of session `target_sid`. Broadcasts
    `message_ask_result_changed` so any open view of `target_sid` renders
    the picker inline on that turn.

    Shared by the Ask flow (target = the stable Ask session) and the
    `propose_sessions` MCP tool (target = the calling session).

    `proposed_project_path` is an optional hint for the New Session modal;
    validated against `project_store` and dropped when unknown."""
    result, event = _apply_proposed_sessions(
        session_ids,
        reasoning,
        target_sid=target_sid,
        msg_id=msg_id,
        proposed_project_path=proposed_project_path,
    )
    if event is not None:
        _broadcast_global_later("message_ask_result_changed", event)
    return result


def _apply_proposed_sessions(
    session_ids: list,
    reasoning: str,
    *,
    target_sid: str,
    msg_id: str,
    proposed_project_path: str = "",
    error: str = "",
) -> tuple[dict, dict | None]:
    proj_path, proj_node = _resolve_proposed_project(proposed_project_path)
    result = {
        "session_ids": validate_proposed(session_ids),
        "reasoning": reasoning if isinstance(reasoning, str) else "",
        "proposed_project_path": proj_path,
        "proposed_project_node_id": proj_node,
        "error": error if isinstance(error, str) else "",
    }
    virtual_target = virtual_session_store.get(target_sid)
    if virtual_target:
        virtual_session_store.update_message_fields(
            str(virtual_target.get("extension_id") or ""),
            target_sid,
            msg_id,
            {"ask_result": result},
        )
        event = {
            "session_id": target_sid,
            "msg_id": msg_id,
            "ask_result": result,
        }
    else:
        session_manager.set_msg_ask_result(target_sid, msg_id, result)
        event = {
            "session_id": target_sid,
            "msg_id": msg_id,
            "ask_result": result,
        }
    return result, event


# ── Search worker (the shared ranking engine) ───────────────────────────
#
# `run_search_sessions_session` runs on a provisioned session via the
# generic `provisioning` framework: one hidden base session is primed once
# (its provision prompt loads the `search-in-sessions` skill + the JSON-
# answer contract, then responds "ready"), and each call forks that base so
# the fork only carries the real query and greps the transcripts. The base
# persists across searches; forks are ephemeral. Both the Ask flow and the
# session-bridge `search_sessions` MCP tool call it. The base is excluded
# from session-bridge tools in the runner (recursion prevention); the
# provision prompt also forbids calling any session-finding tool.

# Worker cwd = the BC repo root so claude loads `.claude/skills/search-in-
# sessions`. The worker only greps an absolute path, so the cwd is inert
# otherwise.
_REPO_ROOT = Path(__file__).resolve().parent.parent

# Match the last balanced {...} object in the worker's reply.
_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


def _parse_worker_result(text: str) -> Optional[dict]:
    """Extract the JSON object `{session_ids, reasoning}` from the worker's
    reply text. Returns None when no valid object parses."""
    if not text:
        return None
    # The reply may surround the JSON with prose; take the last {...} span.
    m = _JSON_OBJECT_RE.search(text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or "session_ids" not in obj:
        return None
    return obj


class SessionSearchSpec(ProvisionedSessionSpec):
    """Provisioned-session spec for the session-search ranking worker."""

    key = SEARCH_WORKER_MODE
    version = 2
    name = "search-worker"
    env_prefix = "SESSION_SEARCH"
    task_key = "session_search_worker"
    orchestration_mode = "native"
    bare_config = False             # load skills (search-in-sessions) + CLAUDE.md
    worker_creation_policy = "deny"  # isolated grep worker — no sub-workers
    machine_completion = False      # tool-using (grep): normal prompt path
    run_mode = "fork"
    dispatch = "in_process"
    on_no_fork = "error"
    default_cwd = str(_REPO_ROOT)
    # Base only ever holds the provision turn; a leaked query would show as a
    # 2nd user turn (caught by turn-count). Skill content can be sizable.
    dirty_policy = DirtyPolicy(
        max_base_bytes=1_000_000,
        max_user_turns=1,
        max_assistant_turns=1,
    )

    def build_provision_prompt(self, ctx: dict) -> str:
        """One-time priming: load the grep methodology + JSON-answer contract
        so each fork only needs the raw query. No greping during provision."""
        sessions_dir = ctx.get("sessions_dir") or str(ba_home() / "sessions")
        return render_prompt("search_worker.md", {"sessions_dir": sessions_dir})

    def build_instructions(self, query: str, ctx: dict) -> str:
        # Methodology + JSON contract live in the provision prompt; the fork
        # only needs the raw request.
        return query

    def parse_result(self, text: str, ctx: dict) -> dict:
        reported = _parse_worker_result(text)
        if not isinstance(reported, dict):
            return {"error": "parse_failed"}
        return reported


SEARCH_SPEC = provisioning.register(SessionSearchSpec())


async def run_search_sessions_session(
    query: str,
    *,
    propose: bool = False,
    propose_target: Optional[str] = None,
    propose_msg_id: Optional[str] = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    max_results: int = _DEFAULT_MAX_RESULTS,
    include_worker_events: bool = False,
) -> dict:
    """Run one provisioned search-worker fork and return the ranked ids.

    Forks the provisioned search base (primed once with the grep methodology
    + JSON-answer contract) so the fork only carries the raw query, greps
    the transcripts, and answers in JSON. The base persists across calls;
    the fork is ephemeral. When `propose` is set, also stamps the picker on
    `propose_target`/`propose_msg_id` in the same call.

    Returns `{session_ids, reasoning, error}`; `error` is one of `None`,
    `"empty_query"`, `"timeout"`, `"dispatch_failed"`, `"parse_failed"`.
    """
    if not query or not query.strip():
        return {"session_ids": [], "reasoning": "", "error": "empty_query"}
    query = query.strip()

    ctx = {
        "sessions_dir": str(ba_home() / "sessions"),
        "max_results": max_results,
    }
    try:
        result = await asyncio.wait_for(
            provisioning.run(SEARCH_SPEC, query, ctx),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return {"session_ids": [], "reasoning": "", "error": "timeout"}
    except Exception:
        logger.exception("run_search_sessions_session: provisioned dispatch failed")
        return {"session_ids": [], "reasoning": "", "error": "dispatch_failed"}

    reported = result.value
    if not isinstance(reported, dict) or reported.get("error"):
        return {"session_ids": [], "reasoning": "", "error": "parse_failed"}

    session_ids = validate_proposed(reported.get("session_ids") or [])[:max_results]
    reasoning = reported.get("reasoning", "")
    if not isinstance(reasoning, str):
        reasoning = ""

    if propose and propose_target and propose_msg_id:
        propose_sessions(
            session_ids, reasoning,
            target_sid=propose_target, msg_id=propose_msg_id,
        )

    out = {
        "session_ids": session_ids,
        "reasoning": reasoning,
        "error": None,
    }
    if include_worker_events:
        out["_worker_events"] = result.dispatch_result.get("events") or []
    return out



# ── Stable Ask session (UI container) ───────────────────────────────────


_ensure_lock = asyncio.Lock()


def _broadcast_global_later(event_type: str, data: dict) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _run() -> None:
        try:
            from orchestrator import get_active_coordinator

            coordinator = get_active_coordinator()
            if coordinator is not None:
                await coordinator.broadcast_global(event_type, data)
        except Exception:
            logger.exception("ask virtual broadcast failed type=%s", event_type)

    loop.create_task(_run())


async def _broadcast_ask_session_updated() -> None:
    from orchestrator import get_active_coordinator

    coordinator = get_active_coordinator()
    if coordinator is None:
        return
    session = await asyncio.to_thread(virtual_session_store.get, ASK_SINGLETON_ID)
    if session:
        await coordinator.broadcast_global(
            "session_metadata_updated",
            {"session_id": ASK_SINGLETON_ID, "patch": session},
        )


def _broadcast_ask_running(value: bool) -> None:
    """Flip the Ask session's running badge so the UI shows the search
    worker turn in flight (it takes ~30-40s). Wire-only metadata ping —
    the Ask session never enters `_run_state`, so the normal
    `running_changed` recompute path can't fire for it; this ping carries
    the value directly. Outside `msg.events` / `events.jsonl`, like the
    other Ask metadata broadcasts (convergence invariant does not apply)."""
    _broadcast_global_later("session_running_changed", {
        "session_id": ASK_SINGLETON_ID,
        "value": value,
        "cwd": str(_REPO_ROOT),
        "node_id": "primary",
    })


async def ensure_ask_session() -> dict:
    """Lazy-create the stable Ask session — the UI container that
    accumulates search turns. Hidden from the sidebar and from
    `_build_index`. The Ask session runs NO provider turns itself; local
    search produces the rendered turns (user query + assistant reasoning +
    picker).

    Race-safe: an `asyncio.Lock` serializes get-or-create.
    """
    async with _ensure_lock:
        sess = await asyncio.to_thread(virtual_session_store.get, ASK_SINGLETON_ID)
        if sess is not None:
            return sess
        def _create() -> dict:
            _ask_llm = config_store.resolve_internal_llm("session_search_worker")
            return virtual_session_store.upsert(
                ASK_EXTENSION_ID,
                {
                    "id": ASK_SINGLETON_ID,
                    "name": "Ask",
                    "cwd": str(_REPO_ROOT),
                    "model": _ask_llm["model"],
                    "provider_id": _ask_llm["provider_id"],
                    "node_id": "primary",
                    "messages": [],
                    "metadata": {"working_mode": ASK_SINGLETON_MODE},
                },
            )

        return await asyncio.to_thread(_create)


# Latest-wins: a new Ask search cancels a prior in-flight one so worker
# turns don't pile up when the user fires several searches.
_inflight: Optional[asyncio.Task] = None
_inflight_lock = asyncio.Lock()


def find_user_message_by_client_id(client_id: Optional[str]) -> Optional[dict]:
    if not client_id:
        return None
    sess = virtual_session_store.get(ASK_SINGLETON_ID)
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


@perf.timed_fn("ask.search")
async def search(
    query: str,
    *,
    client_id: Optional[str] = None,
    lifecycle_msg_id: Optional[str] = None,
    on_user_message: Optional[Callable[[dict], Awaitable[None]]] = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict:
    """Ask UI/REST entry. Append the query as a user turn on the stable Ask
    session, run a search worker, then append an assistant turn carrying
    the reasoning + the propose picker. Returns
    `{session_ids, reasoning, error}`.

    Concurrency: latest call wins — a prior in-flight search is cancelled.
    """
    if not query or not query.strip():
        return {"session_ids": [], "reasoning": "", "error": "empty_query"}

    global _inflight
    async with _inflight_lock:
        if await asyncio.to_thread(find_user_message_by_client_id, client_id):
            return {
                "session_ids": [],
                "reasoning": "",
                "error": "duplicate_client_id",
            }
        prior = _inflight
        if prior is not None and not prior.done():
            prior.cancel()
        task = asyncio.create_task(
            _ask_search(
                query.strip(),
                client_id=client_id,
                lifecycle_msg_id=lifecycle_msg_id,
                on_user_message=on_user_message,
                timeout=timeout,
                max_results=max_results,
            ),
            name="ask_search",
        )
        _inflight = task
    try:
        return await task
    except asyncio.CancelledError:
        return {"session_ids": [], "reasoning": "", "error": "cancelled"}


# Friendly text shown inside the Ask picker when the search worker can't
# return a usable answer. The picker (with its Create-new / Never-mind
# actions) is the home for these outcomes — they are NOT rendered as a red
# "Failed" assistant bubble.
_ASK_ERROR_MESSAGES = {
    "parse_failed": "Couldn't read a clear answer from the search. "
                    "Start a new session with your prompt, or never mind.",
    "timeout": "The search timed out. "
               "Start a new session with your prompt, or never mind.",
    "dispatch_failed": "The search couldn't run. "
                       "Start a new session with your prompt, or never mind.",
}


def _ask_error_message(error_code: object) -> str:
    """Map an internal search-worker error code to user-facing picker text.
    Returns '' for no error / unknown soft codes (the picker just shows the
    empty 'No related sessions found' state with its actions)."""
    if not error_code:
        return ""
    return _ASK_ERROR_MESSAGES.get(str(error_code), "")


def _ask_assistant_message_from_worker_result(result: dict) -> dict:
    worker_events = _render_events_from_worker_result(result)
    content = (
        extract_output_text(strip_synthetic_events(worker_events))
        if worker_events else ""
    )
    if not content:
        content = str(result.get("reasoning") or "")
    # The Ask bubble shows only the worker's answer text + the picker — not
    # the worker fork's internal transcript. That transcript carries the
    # inherited provision exchange (the "ready" priming reply) plus every
    # grep tool_use, which leaks as noise into the Ask turn. The worker's
    # own event log is retained in the worker panel/provenance; it must not
    # be grafted onto the Ask message's `events`.
    # The Ask turn never renders as a red error bubble: any worker error is
    # surfaced inside the picker (see `_ask_error_message`). The assistant
    # message is always a completed, non-error turn.
    msg = {
        "id": uuid.uuid4().hex,
        "role": "assistant",
        "content": content,
        "events": [],
        "timestamp": datetime.now().isoformat(),
        "isStreaming": False,
        "completed_at": datetime.now().isoformat(),
    }
    return msg


def _render_events_from_worker_result(result: dict) -> list[dict]:
    raw = result.get("_worker_events")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for event in raw:
        if not isinstance(event, dict):
            continue
        if event.get("type") not in ("agent_message", "manager_event"):
            continue
        out.append(copy.deepcopy(event))
    return strip_synthetic_events(out)


async def _ask_search(
    query: str,
    *,
    client_id: Optional[str],
    lifecycle_msg_id: Optional[str],
    on_user_message: Optional[Callable[[dict], Awaitable[None]]],
    timeout: float,
    max_results: int,
) -> dict:
    """Append the user turn, run the worker, append the assistant turn +
    picker on the stable Ask session."""
    await ensure_ask_session()
    user_msg = {
        "id": uuid.uuid4().hex,
        "role": "user",
        "content": query,
        "timestamp": datetime.now().isoformat(),
        "client_id": client_id,
        "lifecycle_msg_id": lifecycle_msg_id,
    }
    await asyncio.to_thread(
        virtual_session_store.append_message,
        ASK_EXTENSION_ID,
        ASK_SINGLETON_ID,
        user_msg,
    )
    if on_user_message is not None:
        await on_user_message(user_msg)
    await _broadcast_ask_session_updated()

    _broadcast_ask_running(True)
    try:
        result = await run_search_sessions_session(
            query,
            timeout=timeout,
            max_results=max_results,
            include_worker_events=True,
        )

        assistant_msg = _ask_assistant_message_from_worker_result(result)
        asst_msg_id = assistant_msg["id"]
        await asyncio.to_thread(
            virtual_session_store.append_message,
            ASK_EXTENSION_ID,
            ASK_SINGLETON_ID,
            assistant_msg,
        )
        # Always stamp the picker — even with no matches or on a worker error —
        # so the user gets the Create-new / Never-mind actions and any error is
        # shown inside the picker rather than as a red "Failed" bubble.
        _, event = await asyncio.to_thread(
            _apply_proposed_sessions,
            result.get("session_ids") or [], result.get("reasoning", ""),
            target_sid=ASK_SINGLETON_ID, msg_id=asst_msg_id,
            error=_ask_error_message(result.get("error")),
        )
        if event is not None:
            _broadcast_global_later("message_ask_result_changed", event)
        await _broadcast_ask_session_updated()
        return {k: v for k, v in result.items() if not k.startswith("_")}
    finally:
        # Latest-wins: a newer search may have cancelled this task and taken
        # over `_inflight`. Only clear the running badge if this task is still
        # the active one — otherwise we'd snuff the newer search's indicator.
        if _inflight is asyncio.current_task():
            _broadcast_ask_running(False)


def set_ask_choice(msg_id: str, chosen_session_id: Optional[str]) -> Optional[dict]:
    updated, event = _apply_ask_choice(msg_id, chosen_session_id)
    if event is not None:
        _broadcast_global_later("message_ask_choice_changed", event)
    return updated


async def set_ask_choice_async(
    msg_id: str,
    chosen_session_id: Optional[str],
) -> Optional[dict]:
    updated, event = await asyncio.to_thread(
        _apply_ask_choice,
        msg_id,
        chosen_session_id,
    )
    if event is not None:
        _broadcast_global_later("message_ask_choice_changed", event)
    return updated


def _apply_ask_choice(
    msg_id: str,
    chosen_session_id: Optional[str],
) -> tuple[Optional[dict], dict | None]:
    try:
        updated = virtual_session_store.update_message_fields(
            ASK_EXTENSION_ID,
            ASK_SINGLETON_ID,
            msg_id,
            {"chosen_session_id": chosen_session_id},
        )
    except KeyError:
        return None, None
    if updated is not None:
        return updated, {
            "session_id": ASK_SINGLETON_ID,
            "msg_id": msg_id,
            "chosen_session_id": chosen_session_id,
        }
    return updated, None
