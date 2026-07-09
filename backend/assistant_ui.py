"""Assistant extension core substrate.

The assistant surface has two persistent native sessions backed by one shared
assistant store: a user-facing ``Assistant`` and a hidden ``Assistant Monitor``.
Their role prompts + stateless board preamble are delivered via per-session
``capability_contexts``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from pathlib import Path
from typing import Any

import extension_store
import paths
import session_bridge
import session_search
from session_manager import manager as session_manager

_LOCK = threading.Lock()
ASSISTANT_NAME = "Assistant"
MONITOR_NAME = "Assistant Monitor"
MONITOR_WORKING_MODE = "assistant_monitor"

# Worker cwd: the BC repo root. The board fork does no filesystem work
# (bare_config — no skills, machine_completion — no tools), so this is inert,
# but a deterministic absolute cwd keeps the provisioned-session registry key
# stable across calls.
def _ext_id() -> str | None:
    return extension_store.BUILTIN_ASSISTANT_EXTENSION_ID


def _state_path() -> Path:
    return paths.ba_home() / "assistant_singleton.json"


def _install_path() -> Path | None:
    eid = _ext_id()
    if not eid:
        return None
    return extension_store.runtime_package_root(eid)


def _system_prompt() -> str:
    path = (_install_path() or Path(".")) / "prompts" / "system.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _monitor_prompt() -> str:
    path = (_install_path() or Path(".")) / "prompts" / "monitor.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# Every provider KIND the assistant session can run on. The role prompt is
# provider-agnostic, so the same content is attached to every kind;
# `provider_capability_contexts` selects per turn by the runner's provider_kind,
# so an output must exist for each kind the session may use. This list is the
# comprehensive fallback (and the stability anchor): the live registry is unioned
# in, but since every registry KIND is already here, the merged set is constant
# across calls → the capability_contexts hash stays byte-stable (no cache churn)
# and coverage never depends on the registry being loaded. New provider KINDs
# must be added here.
_FALLBACK_PROVIDER_KINDS = (
    "claude", "codex", "gemini", "openai",
    "agy", "fugu", "claude-remote", "copilot",
)


def _provider_kinds() -> list[str]:
    """Deterministic list of provider KINDs to attach the role prompt to.

    The fallback list already covers every known KIND, so the registry is only
    a safety net for a brand-new provider not yet listed above. Sorted so the
    capability_contexts hash is byte-stable regardless of registry order."""
    kinds: list[str] = []
    try:
        from provider import known_providers
        for prov in known_providers():
            kind = getattr(prov, "KIND", "")
            if isinstance(kind, str) and kind:
                kinds.append(kind)
    except Exception:  # noqa: BLE001 — registry unavailable in some test contexts
        kinds = []
    merged: list[str] = []
    for kind in [*kinds, *_FALLBACK_PROVIDER_KINDS]:
        if kind not in merged:
            merged.append(kind)
    return sorted(merged)


def _build_role_capability_contexts(
    *,
    base_content: str,
    board_preamble: str = "",
    source_id: str,
    capability_id: str,
    name: str,
) -> list[dict]:
    from capability_contexts import MAX_CAPABILITY_CONTENT_CHARS
    content = str(base_content or "")
    if board_preamble:
        content = f"{content}\n\n{board_preamble}" if content else board_preamble
    if not content.strip():
        return []
    if len(content) > MAX_CAPABILITY_CONTENT_CHARS:
        content = content[:MAX_CAPABILITY_CONTENT_CHARS]
    outputs = [{"provider_kind": kind, "content": content} for kind in _provider_kinds()]
    return [{
        "source_id": source_id,
        "capability_id": capability_id,
        "name": name,
        "category": "role",
        "outputs": outputs,
    }]


def build_capability_contexts(board_preamble: str = "") -> list[dict]:
    return _build_role_capability_contexts(
        base_content=_system_prompt(),
        board_preamble=board_preamble,
        source_id="assistant",
        capability_id="assistant-role",
        name=ASSISTANT_NAME,
    )


def build_monitor_capability_contexts(board_preamble: str = "") -> list[dict]:
    return _build_role_capability_contexts(
        base_content=_monitor_prompt(),
        board_preamble=board_preamble,
        source_id="assistant-monitor",
        capability_id="assistant-monitor-role",
        name=MONITOR_NAME,
    )


def _read_state() -> dict:
    path = _state_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_state(data: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def cleanup_singleton() -> None:
    with _LOCK:
        state = _read_state()
        for key, name in (("session_id", ASSISTANT_NAME), ("monitor_session_id", MONITOR_NAME)):
            sid = state.get(key)
            sess = session_manager.get(sid) if sid else None
            if (
                sess is not None
                and sess.get("source") == "extension"
                and sess.get("name") == name
            ):
                session_manager.delete(sess["id"])
        _state_path().unlink(missing_ok=True)


def _caps_hash(caps: list[dict]) -> str:
    raw = json.dumps(caps, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _existing_singleton_session(
    *,
    name: str,
    user_initiated: bool,
    working_mode: str | None = None,
) -> dict | None:
    candidates = [
        sess
        for sess in session_manager.list()
        if sess.get("source") == "extension"
        and sess.get("name") == name
        and sess.get("user_initiated") is user_initiated
        and sess.get("kind", "user") == "user"
        and not sess.get("parent_session_id")
        and (working_mode is None or sess.get("working_mode") == working_mode)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda sess: (str(sess.get("created_at") or ""), str(sess.get("id") or "")))
    sid = candidates[0].get("id")
    return session_manager.get(str(sid)) if sid else None


def _ensure_role_session(
    *,
    state_key: str,
    hash_key: str,
    name: str,
    user_initiated: bool,
    board_preamble: str | None,
    caps_builder,
    working_mode: str | None = None,
) -> dict:
    with _LOCK:
        eid = _ext_id()
        if not eid:
            raise RuntimeError("assistant extension id not loaded (private registry absent)")
        state = _read_state()
        sid = state.get(state_key)
        sess = session_manager.get(sid) if sid else None
        if sess is None:
            sess = _existing_singleton_session(
                name=name,
                user_initiated=user_initiated,
                working_mode=working_mode,
            )
        if board_preamble is None:
            board_preamble = str(state.get("board_preamble") or "")
        else:
            board_preamble = str(board_preamble or "")
        caps = caps_builder(board_preamble)
        cap_hash = _caps_hash(caps)
        next_state = {
            **state,
            "board_preamble": board_preamble,
            hash_key: cap_hash,
        }
        if sess is None:
            sess = session_manager.create(
                name=name,
                orchestration_mode="native",
                source="extension",
                user_initiated=user_initiated,
                capability_contexts=caps,
            )
            if working_mode:
                import working_mode as working_mode_module
                sess = working_mode_module.mark_working_mode(
                    sess["id"],
                    mode=working_mode,
                    meta={"role": "assistant_monitor"},
                ) or sess
            session_manager.set_name_locked(sess["id"], True)
            next_state[state_key] = sess["id"]
            _write_state(next_state)
        else:
            if sess.get("source") != "extension" or sess.get("user_initiated") is not user_initiated:
                sess = session_manager.set_origin(
                    sess["id"],
                    source="extension",
                    user_initiated=user_initiated,
                ) or sess
            if working_mode and sess.get("working_mode") != working_mode:
                import working_mode as working_mode_module
                sess = working_mode_module.mark_working_mode(
                    sess["id"],
                    mode=working_mode,
                    meta={"role": "assistant_monitor"},
                ) or sess
            current_caps = sess.get("capability_contexts")
            if not isinstance(current_caps, list) or _caps_hash(current_caps) != cap_hash:
                sess = session_manager.set_capability_contexts(sess["id"], caps) or sess
            if not sess.get("name_locked"):
                sess = session_manager.set_name_locked(sess["id"], True) or sess
            if sess.get("name") != name:
                sess = session_manager.rename(sess["id"], name, force=True) or sess
            next_state[state_key] = sess["id"]
            if next_state != state:
                _write_state(next_state)
        # Activate the role capability id the MCP manifest predicate gates on
        # (`contains: {active_capability_ids: ...}`). set_capability_contexts only
        # delivers the role prompt; without the active id the role's gated MCP
        # server never launches. Idempotent — appends only when absent.
        for cap in caps:
            cid = str((cap or {}).get("capability_id") or "").strip()
            if cid:
                sess = session_manager.add_active_capability(sess["id"], cid) or sess
        # Role sessions live outside any project cwd but must show up in every
        # project's session list; `session_matches_project` honors this flag.
        if not sess.get("all_projects"):
            sess = session_manager.set_all_projects(sess["id"], True) or sess
        return sess


def ensure_singleton(board_preamble: str | None = None) -> dict:
    return _ensure_role_session(
        state_key="session_id",
        hash_key="capability_contexts_hash",
        name=ASSISTANT_NAME,
        user_initiated=True,
        board_preamble=board_preamble,
        caps_builder=build_capability_contexts,
    )


def ensure_monitor(board_preamble: str | None = None) -> dict:
    return _ensure_role_session(
        state_key="monitor_session_id",
        hash_key="monitor_capability_contexts_hash",
        name=MONITOR_NAME,
        user_initiated=False,
        board_preamble=board_preamble,
        caps_builder=build_monitor_capability_contexts,
        working_mode=MONITOR_WORKING_MODE,
    )


def _msg_text(message: dict | None) -> str:
    if not message:
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def last_turn(sid: str) -> dict:
    """Compact last-turn view of a session: the user prompt + the assistant's
    reply (the 'next/successor' message) + cwd. Used by the post-turn hook to
    feed the board-update fork without hauling the whole transcript."""
    sess = session_manager.get(sid) or {}
    messages = sess.get("messages") or []
    last_assistant = next((m for m in reversed(messages) if m.get("role") == "assistant"), None)
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    assistant = last_assistant or {}
    return {
        "turn_id": assistant.get("id") or sid,
        "ts": assistant.get("ts"),
        "user_prompt": _msg_text(last_user),
        "user_source": (last_user or {}).get("source"),
        "assistant_message": _msg_text(last_assistant),
        "cwd": sess.get("cwd"),
        "delegated_to": None,
    }


async def search(query: str, *, max_results: int = 10) -> dict:
    """Find ranked Better Agent sessions using the shared search worker."""
    flow = await session_search.run_search_sessions_session(
        query,
        max_results=max_results,
    )
    return await asyncio.to_thread(session_search.canonical_search_response, flow)


async def resolve_ba_session(native_session_id: str) -> dict:
    """Map a session id returned by ``query_provider_native_transcript_index`` (a PROVIDER
    native/agent session id for claude/codex/gemini, or already a BA id for the
    better-agent runner) to the Better Agent session id that ask/delegate operate
    on. Returns ``{"ba_session_id": <app id>}`` or ``{"ba_session_id": None}``
    when the transcript belongs to no BA session (raw native history never run
    through Better Agent — the caller must create a session to act on it)."""
    sid = (native_session_id or "").strip()
    if not sid:
        return {"ba_session_id": None}
    root = await asyncio.to_thread(session_manager.root_id_for, sid)
    return {"ba_session_id": root}


def _norm_path(p: str) -> str:
    if not p:
        return ""
    try:
        return str(Path(p).expanduser().resolve())
    except OSError:
        return str(Path(p).expanduser())


def _adopt_by_import(transcript_path: str, native_id: str) -> dict:
    """Find the exact native session and import it. Match on the transcript
    PATH — the only provider-agnostic key: for codex the FTS ``sid`` is the
    rollout-file stem while the enumerator's ``native_id`` is the codex DB thread
    id, so a native_id match would miss codex; ``jsonl_path`` equals the FTS
    ``path`` for every file-based provider (claude / codex-rollout / gemini /
    agy). ``native_id`` is only a claude fallback when no path is given."""
    import native_import
    want = _norm_path(transcript_path)
    fallback = None
    for sess in native_import.enumerate_native_sessions():
        if want and _norm_path(sess.jsonl_path) == want:
            # Idempotent: a session already imported returns its existing id.
            return {"ba_session_id": native_import.import_session(sess)}
        if not want and native_id and sess.native_id == native_id:
            fallback = sess
    if fallback is not None:
        return {"ba_session_id": native_import.import_session(fallback)}
    return {"ba_session_id": None, "error": "native_session_not_found"}


async def adopt_native_session(native_session_id: str, transcript_path: str = "") -> dict:
    """Bring a native session that has NO Better Agent session into BA so it can
    be acted on: import its transcript into a new BA ``native`` session
    (preserving the full conversation as context) and return that BA session id.

    Pass ``transcript_path`` (the ``path`` column from the search row) — it is the
    accurate, provider-agnostic key; ``native_session_id`` alone is reliable only
    for claude. Idempotent: an already-BA-managed or already-imported session
    returns its existing id — never a duplicate. Returns
    ``{"ba_session_id": None, "error": ...}`` when the session can't be found."""
    sid = (native_session_id or "").strip()
    path = (transcript_path or "").strip()
    if not sid and not path:
        return {"ba_session_id": None, "error": "session_id_or_path_required"}
    if sid:
        root = await asyncio.to_thread(session_manager.root_id_for, sid)
        if root:
            return {"ba_session_id": root}
    return await asyncio.to_thread(_adopt_by_import, path, sid)


async def delegate(target_sid: str, prompt: str) -> dict:
    """Send a prompt to a target session and run its turn; returns the
    session_bridge result (final assistant message + metadata). The target does
    the work in the background; the caller does not block on the UI thread."""
    return await session_bridge.run_for_extension(target_sid, prompt, source="assistant")
