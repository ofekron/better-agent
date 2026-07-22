"""Session search and Ask picker state.

Two shared utilities back every search surface:

  - `run_search_sessions_session(query, *, propose=..., propose_target=,
     propose_msg_id=, timeout=, max_results=)` — the ranking engine. Runs
     on a provisioned session: the backend first builds a bounded candidate
     list from the session index/transcript snippets, then one hidden base
     session is forked per call to rank only those candidates and answer JSON.
     The base persists across searches; the per-call forks are ephemeral. When
     `propose` is set it also stamps the picker on the target in the same call (a "batch"
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
from provisioning import DirtyPolicy, ProvisionedSessionSpec
from prompt_templates import render_prompt
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
# headless. This is the binding budget (the asyncio.wait_for around
# provisioning.run); private session-bridge MCP transport timeouts must exceed
# it so they never preempt the search.
_DEFAULT_TIMEOUT_SECONDS = 15 * 60
_SEARCH_CANDIDATE_LIMIT = 40
_SEARCH_SNIPPET_LIMIT = 360
_SEARCH_CONTENT_INDEX_MAX_WAIT_SECONDS = 0.05


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
        if not data.get("user_initiated"):
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
            # Filter dimensions. Coerced to "" so filter matching is
            # straightforward (a missing value never equals a real one).
            "provider_id": data.get("provider_id") or "",
            "model": data.get("model") or "",
            "reasoning_effort": data.get("reasoning_effort") or "",
            "node_id": data.get("node_id") or "primary",
            "folder_id": data.get("folder_id") or "",
            "tag_ids": [
                tag.get("id")
                for tag in data.get("session_tags") or []
                if isinstance(tag, dict) and isinstance(tag.get("id"), str)
            ],
        })
    # Most-recently-updated first — gives the model a useful prior when
    # multiple sessions look similar.
    index.sort(
        key=lambda s: session_store.timestamp_sort_value(s.get("updated_at")),
        reverse=True,
    )
    return index


_SEARCH_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_./:-]*", re.IGNORECASE)


def _search_tokens(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in _SEARCH_TOKEN_RE.finditer((text or "").lower()):
        token = match.group(0)
        if len(token) < 2 or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(part for part in parts if part)


def _session_snippet(session: dict, tokens: list[str]) -> str:
    messages = session.get("messages")
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        text = _content_text(msg.get("content")).strip()
        if not text:
            continue
        lower = text.lower()
        if tokens and not any(token in lower for token in tokens):
            continue
        text = " ".join(text.split())
        return text[:_SEARCH_SNIPPET_LIMIT]
    return ""


def _candidate_score(row: dict, tokens: list[str]) -> int:
    if not tokens:
        return 0
    weighted_fields = (
        (str(row.get("name") or ""), 5),
        (str(row.get("first_user_prompt") or ""), 5),
        (str(row.get("project_name") or ""), 3),
        (str(row.get("cwd") or ""), 2),
        (str(row.get("id") or ""), 1),
    )
    score = 0
    for text, weight in weighted_fields:
        lower = text.lower()
        for token in tokens:
            if token in lower:
                score += weight
    return score


def _candidate_payload(
    row: dict,
    tokens: list[str],
    *,
    snippet: str = "",
) -> dict[str, Any]:
    payload = {
        "id": str(row.get("id") or ""),
        "name": str(row.get("name") or ""),
        "cwd": str(row.get("cwd") or ""),
        "project_name": str(row.get("project_name") or ""),
        "first_user_prompt": str(row.get("first_user_prompt") or ""),
        "updated_at": str(row.get("updated_at") or ""),
    }
    if not snippet:
        session = session_store.get_session(payload["id"])
        if isinstance(session, dict):
            snippet = _session_snippet(session, tokens)
    if snippet and snippet != payload["first_user_prompt"]:
        payload["matching_snippet"] = snippet
    return payload


def _search_candidates(
    query: str,
    *,
    filters: Optional[dict] = None,
    limit: int = _SEARCH_CANDIDATE_LIMIT,
) -> list[dict[str, Any]]:
    tokens = _search_tokens(query)
    if not tokens:
        return []
    rows = _build_index()
    if filters:
        rows = [row for row in rows if _matches_filters(row, filters)]
    metadata_scored: list[tuple[int, dict, str]] = []
    for row in rows:
        score = _candidate_score(row, tokens)
        if score > 0:
            metadata_scored.append((score, row, ""))
    scored = metadata_scored
    if len(metadata_scored) < limit:
        content_scores: dict[str, int] = {}
        try:
            import session_search_index
            content_scores = {
                str(item.get("session_id")): int(item.get("score") or 0)
                for item in session_search_index.search(
                    query,
                    limit=max(len(rows), limit),
                    max_wait_seconds=_SEARCH_CONTENT_INDEX_MAX_WAIT_SECONDS,
                )
                if item.get("session_id")
            }
        except Exception:
            logger.debug("_search_candidates: content index lookup failed", exc_info=True)
        metadata_ids = {str(row.get("id") or "") for _, row, _ in metadata_scored}
        for row in rows:
            sid = str(row.get("id") or "")
            if sid not in metadata_ids and content_scores.get(sid, 0) > 0:
                scored.append((2, row, ""))
    scored.sort(
        key=lambda item: (
            item[0],
            session_store.timestamp_sort_value(item[1].get("updated_at")),
        ),
        reverse=True,
    )
    return [
        _candidate_payload(row, tokens, snippet=snippet)
        for _, row, snippet in scored[:limit]
    ]


def index_stub_map() -> dict[str, dict]:
    """`{id: stub}` for every listable session. Used to enrich the ids a
    search returns with metadata, and to reject ids that aren't
    real/listable (hidden, ephemeral, singleton) — fail closed."""
    return {s["id"]: s for s in _build_index()}


def canonical_search_response(flow: dict) -> dict:
    """Project an internal ranker result into the public search contract."""
    stubs = index_stub_map()
    results = []
    for sid in flow.get("session_ids") or []:
        stub = stubs.get(sid)
        if not stub:
            continue
        results.append({
            "id": sid,
            "name": stub.get("name", ""),
            "cwd": stub.get("cwd", ""),
            "first_user_prompt": stub.get("first_user_prompt", ""),
        })
    response = {"results": results}
    reasoning = flow.get("reasoning")
    if reasoning:
        response["reasoning"] = reasoning
    error = flow.get("error")
    if error:
        response["error"] = error
    return response


# ── Filters ─────────────────────────────────────────────────────────────
#
# Exact-match filters applied to `_build_index()` entries. Each is optional;
# `None` / "" means "no constraint". Matching is case-sensitive on the
# canonical stored value; tag filters require every requested tag. When any
# filter is set the search worker is constrained to the matching candidate ids
# and its output is post-validated.

_SCALAR_FILTER_KEYS = (
    "provider_id",
    "model",
    "reasoning_effort",
    "node_id",
    "cwd",
    "folder_id",
)
_LIST_FILTER_KEYS = ("tag_ids",)


def _normalize_filters(**raw) -> dict:
    """Drop empty/None values. Returns `{}` when no filter is active."""
    out: dict = {}
    for key in _SCALAR_FILTER_KEYS:
        val = raw.get(key)
        if isinstance(val, str):
            val = val.strip()
        if val:
            out[key] = val
    for key in _LIST_FILTER_KEYS:
        vals = raw.get(key)
        if not isinstance(vals, list):
            continue
        clean = [
            val.strip()
            for val in vals
            if isinstance(val, str) and val.strip()
        ]
        if clean:
            out[key] = clean
    return out


def _matches_filters(stub: dict, filters: dict) -> bool:
    for key in _SCALAR_FILTER_KEYS:
        want = filters.get(key)
        if want and stub.get(key) != want:
            return False
    tag_ids = filters.get("tag_ids")
    if tag_ids:
        have = stub.get("tag_ids")
        if not isinstance(have, list) or not set(tag_ids).issubset(set(have)):
            return False
    return True


def _filtered_candidate_ids(filters: dict) -> list[str]:
    """Ids from `_build_index()` that pass `filters`, newest-first order
    preserved."""
    if not filters:
        return []
    return [s["id"] for s in _build_index() if _matches_filters(s, filters)]


def validate_proposed(
    session_ids: list, *, filters: Optional[dict] = None,
) -> list[str]:
    """Keep only ids that resolve to a real, listable session (drops the
    Ask container itself, hidden/ephemeral workers, and unknown ids). When
    `filters` is given, additionally require each id's index entry to match
    every non-empty filter value (exact, case-sensitive)."""
    if not isinstance(session_ids, list):
        return []
    if filters:
        valid_ids = set(_filtered_candidate_ids(filters))
    else:
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
    validated_ids = validate_proposed(session_ids)
    result = {
        **canonical_search_response({"session_ids": validated_ids}),
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
# generic `provisioning` framework: backend code gathers a bounded candidate
# list, one hidden base session is primed once with the JSON-answer contract,
# and each call forks that base so the fork only ranks those candidates. The
# base persists across searches; forks are ephemeral. Both the Ask flow and the
# session-bridge `search_sessions` MCP tool call it.

# Worker cwd = the BC repo root for stable project identity. The worker is a
# tool-less machine-completion ranker; transcript access happens in backend
# candidate collection before dispatch.
_REPO_ROOT = Path(__file__).resolve().parent.parent

def _parse_worker_result(text: str) -> Optional[dict]:
    """Extract the JSON object `{session_ids, reasoning}` from the worker's
    reply text. Returns None when no valid object parses."""
    if not text:
        return None
    for candidate in _json_object_spans_from_end(text):
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and "session_ids" in obj:
            return obj
    return None


def _json_object_spans_from_end(text: str):
    depth = 0
    end: Optional[int] = None
    in_string = False
    escaped = False
    for idx in range(len(text) - 1, -1, -1):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "}":
            if depth == 0:
                end = idx + 1
            depth += 1
            continue
        if ch != "{":
            continue
        if depth == 0 or end is None:
            continue
        depth -= 1
        if depth == 0:
            yield text[idx:end]
            end = None


class SessionSearchSpec(ProvisionedSessionSpec):
    """Provisioned-session spec for the session-search ranking worker."""

    key = SEARCH_WORKER_MODE
    version = 3
    name = "search-worker"
    env_prefix = "SESSION_SEARCH"
    task_key = "session_search_worker"
    orchestration_mode = "native"
    bare_config = True
    worker_creation_policy = "deny"  # isolated grep worker — no sub-workers
    machine_completion = True
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
        return render_prompt("provisioning/search_worker.md", {})

    def build_instructions(self, query: str, ctx: dict) -> str:
        candidates = ctx.get("candidates") if isinstance(ctx, dict) else []
        if not isinstance(candidates, list):
            candidates = []
        payload = {
            "query": query,
            "max_results": int(ctx.get("max_results") or _DEFAULT_MAX_RESULTS),
            "candidates": candidates,
        }
        return (
            "<session-search-task>\n"
            "Rank only the provided candidate sessions for the query. "
            "Do not answer the query as a task. Do not use tools. "
            "Return exactly one JSON object with session_ids and reasoning.\n"
            f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n"
            "</session-search-task>"
        )

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
    provider_id: Optional[str] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    node_id: Optional[str] = None,
    cwd: Optional[str] = None,
    folder_id: Optional[str] = None,
    tag_ids: Optional[list[str]] = None,
) -> dict:
    """Run one provisioned search-worker fork and return the ranked ids.

    Backend code collects bounded candidate sessions first; the provisioned
    worker ranks only those candidates and answers in JSON. The base persists
    across calls; the fork is ephemeral. When `propose` is set, also stamps
    the picker on `propose_target`/`propose_msg_id` in the same call.

    Optional filters narrow the candidate set BEFORE the worker runs: the worker
    is constrained to the matching ids and its output is post-validated
    against the same filters, so a worker that ignores the constraint still
    cannot surface a filtered-out session. An empty candidate set short-
    circuits with no worker dispatch.

    Returns `{session_ids, reasoning, error}`; `error` is one of `None`,
    `"empty_query"`, `"timeout"`, `"dispatch_failed"`, `"parse_failed"`.
    """
    if not query or not query.strip():
        return {"session_ids": [], "reasoning": "", "error": "empty_query"}
    query = query.strip()

    filters = _normalize_filters(
        provider_id=provider_id,
        model=model,
        reasoning_effort=reasoning_effort,
        node_id=node_id,
        cwd=cwd,
        folder_id=folder_id,
        tag_ids=tag_ids,
    )
    with perf.timed("ask.search_candidates"):
        candidates = await asyncio.to_thread(_search_candidates, query, filters=filters)
    if not candidates:
        return {"session_ids": [], "reasoning": "", "error": None}

    ctx = {
        "max_results": max_results,
        "candidates": candidates,
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

    session_ids = validate_proposed(
        reported.get("session_ids") or [], filters=filters or None,
    )[:max_results]
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
    provider_id: Optional[str] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    node_id: Optional[str] = None,
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
                provider_id=provider_id,
                model=model,
                reasoning_effort=reasoning_effort,
                node_id=node_id,
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
    # The Ask turn is represented entirely by its inline picker footer: the
    # reasoning lives in `ask_result.reasoning`, the matches + actions in the
    # picker. The assistant message itself carries NO body — empty content and
    # no events. Stamping the reasoning into `content` too would render it
    # twice (once as the assistant bubble, once in the picker) and pad the turn
    # with an empty indented block. The worker fork's internal transcript (the
    # inherited "ready" provision reply + every grep tool_use) is noise and
    # stays in the worker panel/provenance only.
    # The Ask turn never renders as a red error bubble: any worker error is
    # surfaced inside the picker (see `_ask_error_message`).
    msg = {
        "id": uuid.uuid4().hex,
        "role": "assistant",
        "content": "",
        "events": [],
        "timestamp": datetime.now().isoformat(),
        "isStreaming": False,
        "completed_at": datetime.now().isoformat(),
    }
    return msg


async def _ask_search(
    query: str,
    *,
    client_id: Optional[str],
    lifecycle_msg_id: Optional[str],
    on_user_message: Optional[Callable[[dict], Awaitable[None]]],
    timeout: float,
    max_results: int,
    provider_id: Optional[str] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    node_id: Optional[str] = None,
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
            provider_id=provider_id,
            model=model,
            reasoning_effort=reasoning_effort,
            node_id=node_id,
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
