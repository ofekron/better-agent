"""Assistant extension core substrate.

The assistant is a single, persistent, **reused native session** the user talks
1-on-1 with. Its optimized prompt + stateless board preamble are delivered via
the session's `capability_contexts` — the existing per-session, per-turn-replayed
system-prompt-append path (no new per-session prompt field, no provider surgery).

This module owns the find-or-create singleton, search (reuses the ask search
worker), delegation (reuses session_bridge), and last-turn extraction. The
board-update classify/rank fork lives elsewhere (TBD); this is the routing tier.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import threading
from pathlib import Path
from typing import Any

import extension_store
import paths
import provisioning
import session_bridge
from session_manager import manager as session_manager
import session_search
from provisioning import DirtyPolicy, ProvisionedSessionSpec

_LOCK = threading.Lock()

# Worker cwd: the BC repo root. The board fork does no filesystem work
# (bare_config — no skills, machine_completion — no tools), so this is inert,
# but a deterministic absolute cwd keeps the provisioned-session registry key
# stable across calls.
_REPO_ROOT = Path(__file__).resolve().parent.parent

# Default per-call ceiling for a board fork. A single classification/extract
# turn is short; bound it here (the spec's own provision_timeout is a per-
# attempt budget) so a stuck fork never wedges the post-turn hook.
_BOARD_TIMEOUT_SECONDS = 5 * 60.0


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


def build_capability_contexts(board_preamble: str = "") -> list[dict]:
    """Capability context appended to the assistant session's system prompt every
    turn. v1: the role prompt; `board_preamble` (stateless item set) is appended
    here once the board mechanism feeds it. State is deliberately NOT included —
    it lives in the volatile tail to keep this cached region byte-stable."""
    content = _system_prompt()
    if board_preamble:
        content = f"{content}\n\n{board_preamble}" if content else board_preamble
    if not content.strip():
        return []
    return [{"name": "Assistant", "category": "role", "content": content}]


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


def _caps_hash(caps: list[dict]) -> str:
    raw = json.dumps(caps, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ensure_singleton(board_preamble: str | None = None) -> dict:
    """Find-or-create the persistent assistant native session and refresh its
    capability_contexts so prompt/preamble edits take effect idempotently.

    `board_preamble` is the stateless item set (ids + descriptions + source
    sessions; no status). When omitted, keep the last known preamble so a bare
    ensure call never wipes the cached board context. Capability contexts are
    written only when their content hash changes, keeping the cached prompt
    prefix byte-stable while the item set is unchanged.
    Returns the live session record."""
    with _LOCK:
        eid = _ext_id()
        if not eid:
            raise RuntimeError("assistant extension id not loaded (private registry absent)")
        state = _read_state()
        sid = state.get("session_id")
        sess = session_manager.get(sid) if sid else None
        if board_preamble is None:
            board_preamble = str(state.get("board_preamble") or "")
        else:
            board_preamble = str(board_preamble or "")
        caps = build_capability_contexts(board_preamble)
        cap_hash = _caps_hash(caps)
        next_state = {
            **state,
            "board_preamble": board_preamble,
            "capability_contexts_hash": cap_hash,
        }
        if sess is None:
            sess = session_manager.create(
                name="Assistant",
                orchestration_mode="native",
                capability_contexts=caps,
            )
            next_state["session_id"] = sess["id"]
            _write_state(next_state)
        elif caps and state.get("capability_contexts_hash") != cap_hash:
            session_manager.set_capability_contexts(sess["id"], caps)
            _write_state(next_state)
        elif next_state != state:
            _write_state(next_state)
        return sess


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
        "assistant_message": _msg_text(last_assistant),
        "cwd": sess.get("cwd"),
        "delegated_to": None,
    }


async def search(query: str, *, max_results: int = 10, timeout: float = 120.0) -> dict:
    """Rank candidate target sessions for a prompt (reuses the ask provisioned
    search worker). Hint-augmentation (comment + source-session map) is a
    follow-up layered on the query."""
    return await session_search.run_search_sessions_session(
        query, max_results=max_results, timeout=timeout
    )


async def delegate(target_sid: str, prompt: str) -> dict:
    """Send a prompt to a target session and run its turn; returns the
    session_bridge result (final assistant message + metadata). The target does
    the work in the background; the caller does not block on the UI thread."""
    return await session_bridge.run_for_extension(target_sid, prompt, source="assistant")


# ── Board-update fork ───────────────────────────────────────────────────
#
# The board fork is the analysis tier: a disposable fork off a primed base
# that classifies a finished target turn into per-item state deltas. It is
# NOT the conversational singleton — the "never fork" rule applies only to
# the main session the user talks to (see plan §3). This mirrors ask /
# session-search: one base is primed once with a STATELESS brief + JSON
# contract (byte-stable → cache-warm) and forked per call; the volatile item
# state + the monitored turn ride the per-fork instruction (the "tail block",
# past the cache boundary — plan §4). State never enters the cached base.

# Match the last balanced {...} object in the fork's reply.
_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")

_BOARD_CONTRACT = (
    "You are the board-update analyzer for the assistant above. You never run "
    "tools and never read files — every input is handed to you in the request. "
    "For each request you will receive a finished session turn and/or a set of "
    "board items with their current state, and you classify what changed.\n\n"
    "Status vocabulary (use these exact strings):\n"
    "- \"open\": work is still in flight.\n"
    "- \"needs_attention\": blocked — cannot proceed without the user.\n"
    "- \"closed\": the work is actually done and verified.\n\n"
    "Always answer with a SINGLE JSON object and nothing after it. The exact "
    "shape required is stated in each request. Never invent an id that was not "
    "given to you; omit an item rather than guess.\n\n"
    "When you have internalized this contract, respond with the single word: ready"
)


def _parse_board_json(text: str) -> dict | None:
    """Extract the last balanced JSON object from a board fork's reply.
    Returns None when nothing parseable is present (caller fails open)."""
    if not text:
        return None
    m = _JSON_OBJECT_RE.search(text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


class AssistantBoardSpec(ProvisionedSessionSpec):
    """Provisioned-session spec for the assistant's board-update analyzer.

    Mirrors `session_search.SessionSearchSpec` (fork off a primed base) but is
    tool-free: `bare_config` (no skills) + `machine_completion` (raw-instruction
    prompt, no team-context wrapper) because the fork only reads text it is
    handed and emits JSON. `default_session` task key → honors the user's
    default internal-LLM provider/model."""

    key = "assistant_board"
    version = 1
    name = "assistant-board-worker"
    env_prefix = "ASSISTANT_BOARD"
    task_key = "default_session"
    orchestration_mode = "native"
    bare_config = True              # pure text→JSON; no skills / CLAUDE.md
    worker_creation_policy = "deny"  # isolated analyzer — no sub-workers
    machine_completion = True       # no tools expected → raw-instruction prompt
    run_mode = "fork"
    dispatch = "in_process"
    on_no_fork = "error"
    default_cwd = str(_REPO_ROOT)
    # Base only ever holds the provision turn ("ready"). A leaked query would
    # show as a 2nd user turn (caught by max_user_turns). The brief embeds the
    # assistant system prompt, so allow a generous base size.
    dirty_policy = DirtyPolicy(
        max_base_bytes=1_000_000,
        max_user_turns=1,
        max_assistant_turns=1,
    )

    def build_provision_prompt(self, ctx: dict) -> str:
        """One-time priming: the assistant role brief + the stateless JSON
        contract. No item state, no turn — those ride each fork's instruction,
        so this base stays byte-stable and cache-warm."""
        brief = _system_prompt().strip()
        parts = []
        if brief:
            parts.append(brief)
        parts.append(_BOARD_CONTRACT)
        return "\n\n---\n\n".join(parts)

    def build_instructions(self, query: str, ctx: dict) -> str:
        # The contract lives in the provision prompt; the fork only needs the
        # per-call request (already a fully-formed instruction string).
        return query

    def parse_result(self, text: str, ctx: dict) -> dict:
        obj = _parse_board_json(text)
        return obj if isinstance(obj, dict) else {"error": "parse_failed"}


BOARD_SPEC = provisioning.register(AssistantBoardSpec())


async def _run_board_fork(instruction: str, *, timeout: float = _BOARD_TIMEOUT_SECONDS) -> dict:
    """Run one board-update fork carrying `instruction`. Returns the parsed
    JSON object, or `{"error": ...}` on dispatch/parse failure. Never raises —
    the post-turn hook must stay best-effort."""
    if not _ext_id():
        return {"error": "assistant_extension_not_loaded"}
    if not instruction.strip():
        return {"error": "empty_instruction"}
    try:
        result = await asyncio.wait_for(
            provisioning.run(BOARD_SPEC, instruction, {}),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return {"error": "timeout"}
    except Exception as exc:  # noqa: BLE001 — fork failure must not crash the hook
        print(f"assistant board fork failed: {exc}", file=sys.stderr, flush=True)
        return {"error": "dispatch_failed"}
    value = result.value
    return value if isinstance(value, dict) else {"error": "parse_failed"}


def _items_block(items: list[dict]) -> str:
    """Compact, deterministic rendering of board items + their current state
    for a fork instruction (the volatile 'tail block')."""
    lines: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        tid = it.get("turn_id") or it.get("id")
        if not tid:
            continue
        lines.append(json.dumps({
            "turn_id": tid,
            "user_prompt": (it.get("user_prompt") or "")[:800],
            "assistant_message": (it.get("assistant_message") or "")[:4000],
            "summary": it.get("summary") or "",
            "status": it.get("status") or "open",
            "cwd": it.get("cwd") or "",
            "delegated_to": it.get("delegated_to") or "",
            "edited_files": (it.get("edited_files") or [])[:50],
        }, ensure_ascii=False))
    return "\n".join(lines)


def _normalize_classifications(obj: dict) -> list[dict]:
    """Coerce a board fork's reply into a list of `{turn_id, status, summary}`
    deltas. Accepts `classifications` (list) and tolerates `id` aliasing
    `turn_id`. Drops malformed rows (fail open)."""
    raw = obj.get("classifications")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        tid = row.get("turn_id") or row.get("id")
        status = row.get("status")
        if not tid or not isinstance(status, str):
            continue
        out.append({
            "turn_id": str(tid),
            "status": status,
            "summary": str(row.get("summary") or ""),
        })
    return out


def _normalize_deltas(obj: dict) -> list[dict]:
    """Coerce an extract-status reply into per-item `{id,status,summary}`
    deltas. Accepts `deltas` and (for classifier reuse) `classifications`."""
    raw = obj.get("deltas")
    if not isinstance(raw, list):
        raw = obj.get("classifications")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        tid = row.get("id") or row.get("turn_id")
        status = row.get("status")
        if not tid or not isinstance(status, str):
            continue
        out.append({
            "id": str(tid),
            "status": status,
            "summary": str(row.get("summary") or ""),
        })
    return out


async def classify(batch: list[dict], *, timeout: float = _BOARD_TIMEOUT_SECONDS) -> dict:
    """Classify a batch of open turns into per-turn `{turn_id, status, summary}`
    deltas via a board fork. `batch` rows are projected turns (turn_id +
    user_prompt + assistant_message + cwd/edited_files). Returns
    `{classifications: [...]}` — empty on empty batch or fork failure."""
    if not batch:
        return {"classifications": []}
    instruction = (
        "Classify each of these in-flight items into its CURRENT status based "
        "on the work captured in its assistant_message. Return ONLY this JSON "
        "object: {\"classifications\": [{\"turn_id\": <id>, \"status\": "
        "\"open|needs_attention|closed\", \"summary\": <=160 chars}]}. One row "
        "per input item, same turn_id.\n\n<items>\n"
        f"{_items_block(batch)}\n</items>"
    )
    obj = await _run_board_fork(instruction, timeout=timeout)
    return {"classifications": _normalize_classifications(obj)}


async def extract_status(
    target_turn: dict,
    items: list[dict],
    *,
    timeout: float = _BOARD_TIMEOUT_SECONDS,
) -> dict:
    """Board-update entry: given a finished monitored target turn and the
    current board item set + state, emit per-item state deltas. This is what
    the post-turn hook fires on a dispatched session's turn completion (plan
    §2/§5). Returns `{deltas: [{id, status, summary}]}` — item-keyed
    transitions only, never aggregate counts (plan §4)."""
    turn_block = json.dumps({
        "source_session": target_turn.get("source_sid") or target_turn.get("session_id") or "",
        "user_prompt": (target_turn.get("user_prompt") or "")[:1200],
        "assistant_message": (target_turn.get("assistant_message") or "")[:6000],
        "cwd": target_turn.get("cwd") or "",
        "edited_files": (target_turn.get("edited_files") or [])[:50],
        "git_commits": (target_turn.get("git_commits") or [])[:20],
    }, ensure_ascii=False)
    instruction = (
        "A monitored target session just finished a turn. Decide how each "
        "OPEN board item's status changed as a result of that turn. Only an "
        "item whose work this turn advanced should change. Return ONLY this "
        "JSON object: {\"deltas\": [{\"id\": <id>, \"status\": "
        "\"open|needs_attention|closed\", \"summary\": <=160 chars}]}. Include "
        "a row only for items that changed; use the exact id from the "
        "board.\n\n<finished_turn>\n"
        f"{turn_block}\n</finished_turn>\n\n<board_items>\n"
        f"{_items_block(items)}\n</board_items>"
    )
    obj = await _run_board_fork(instruction, timeout=timeout)
    return {"deltas": _normalize_deltas(obj)}


async def rank(
    items: list[dict],
    last_topic: dict | None = None,
    *,
    timeout: float = _BOARD_TIMEOUT_SECONDS,
) -> dict:
    """Order open items most-important-first via a board fork (importance is an
    LLM decision; the fork also biases toward finishing the topic already in
    flight — plan §5/§6). Returns `{order: [turn_id, ...]}`. On failure returns
    the input order so the caller's heuristic fallback can take over."""
    ids_in = [it.get("turn_id") for it in items if isinstance(it, dict) and it.get("turn_id")]
    if not ids_in:
        return {"order": []}
    topic_block = json.dumps(last_topic or {}, ensure_ascii=False)
    instruction = (
        "Order these open items most-important-first. Importance wins, but keep "
        "related items adjacent and prefer continuing the topic already in "
        "flight (last_actioned_topic) so the same context stays loaded. Return "
        "ONLY this JSON object: {\"order\": [<turn_id>, ...]} listing EVERY "
        "input turn_id exactly once.\n\n<last_actioned_topic>\n"
        f"{topic_block}\n</last_actioned_topic>\n\n<items>\n"
        f"{_items_block(items)}\n</items>"
    )
    obj = await _run_board_fork(instruction, timeout=timeout)
    order = obj.get("order")
    if not isinstance(order, list):
        return {"order": ids_in}
    seen: set[str] = set()
    cleaned: list[str] = []
    valid = set(ids_in)
    for tid in order:
        tid = str(tid)
        if tid in valid and tid not in seen:
            seen.add(tid)
            cleaned.append(tid)
    # Append any items the fork dropped, preserving input order (never lose one).
    cleaned.extend(tid for tid in ids_in if tid not in seen)
    return {"order": cleaned}
