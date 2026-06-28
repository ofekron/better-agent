"""Tests for cross-provider TODO reconstruction.

Locks the contract for the public Todos extension extractor and the
session-event hook that derives `session.current_todos` from
the event stream — both Claude `TodoWrite` (full-list snapshot) and
Gemini `update_topic` (single-topic delta, mapped to "TodoWrite" by
`runner_gemini`).

Coverage:
  - Claude TodoWrite REPLACE semantics (sample-confirmed: full list
    rewrite every call).
  - Gemini single-topic delta semantics (sample-confirmed: per-call
    `{title, strategic_intent, summary}`, no list, no status).
  - Gemini dedup by `update_topic_<ts>_<n>` source_id; content-hash
    fallback for non-matching ids; skip when `tool_id` empty / input
    non-serializable.
  - Worker_event branch does NOT touch session-level current_todos.
  - Interleaved manager_event + agent_message → last-wins.
  - Convergence invariant: source_is_provider_stream=True vs source_is_provider_stream=False over the same
    event sequence produces byte-equal `current_todos`.
  - Equality precheck suppresses redundant WS frames under recovery
    replay.
  - Extractor purity invariant: `current` items unchanged after call.
  - Post-hydration: `current_todos` is populated even when the on-disk
    session record lacks the field (lazy hydration via apply_event).

Run with:
    cd backend && .venv/bin/python scripts/test_todos_extraction.py
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-todos-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
_REPO = os.path.dirname(_BACKEND)
_TODOS_EXTENSION = os.path.join(_REPO, "extensions", "todos")
if _TODOS_EXTENSION not in sys.path:
    sys.path.insert(0, _TODOS_EXTENSION)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
import extension_store  # noqa: E402
import session_event_extensions  # noqa: E402
from backend.extractor import (  # noqa: E402
    extract_todos_from_normalized,
    extract_tasks_from_normalized,
    derive_current_todos,
    derive_current_tasks,
)

# Seed the isolated test home's extension store the same way production does on
# its first `GET /api/extensions`: `_load()` is a pure read and never installs
# the bundled Todos extension, so without this the builtin-todos session-event
# hook is absent and every apply_event-driven assertion below silently no-ops.
# `list_extensions_with_reconciliation` runs `_ensure_public_extensions`, which
# installs + enables `ofek-dev.todos` as a first-party bundled extension.
extension_store.list_extensions_with_reconciliation(include_hidden=True)
session_event_extensions.invalidate_hook_snapshot()

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


# ─── fixtures ────────────────────────────────────────────────────

def _mk_session(mode: str) -> tuple[str, dict]:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp",
        orchestration_mode=mode, source="cli",
    )
    sid = sess["id"]
    strategy = get_strategy(mode)
    scaffold = strategy.build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, scaffold)
    return sid, scaffold


def _claude_todowrite_native(uuid: str, todos: list, tool_id: str = "tu_1") -> dict:
    """Native-mode shape: raw agent_message carrying a tool_use block."""
    return {
        "type": "agent_message",
        "data": {
            "uuid": uuid,
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "TodoWrite",
                    "input": {"todos": todos},
                }],
            },
        },
    }


def _claude_todowrite_manager(uuid: str, todos: list, tool_id: str = "tu_1") -> dict:
    """Manager-mode shape: agent_message wrapped under manager_event."""
    return {
        "type": "manager_event",
        "data": {"event": _claude_todowrite_native(uuid, todos, tool_id)},
    }


def _gemini_update_topic(
    uuid: str, tool_id: str, title: str, summary: str,
    strategic_intent: str = "",
) -> dict:
    """Gemini's `update_topic` after runner_gemini normalization:
    tool name remapped to "TodoWrite", input keys passed through.
    Native-mode shape (Gemini sessions use native orchestration)."""
    return {
        "type": "agent_message",
        "data": {
            "uuid": uuid,
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "TodoWrite",
                    "input": {
                        "title": title,
                        "summary": summary,
                        "strategic_intent": strategic_intent,
                    },
                }],
            },
        },
    }


def _codex_todo_list(item_id: str, items: list, thread: str = "thread-x") -> dict:
    """Codex `todo_list` stream item run through `runner_codex`
    normalization, then wrapped as the agent_message the tailer feeds
    to apply_event. `event_uuid` is the SAME stable value runner_codex
    derives from (thread_id, item id), so re-emissions REPLACE in place."""
    from runner_codex import _normalize_todo_list, _stable_uuid
    data = _normalize_todo_list(
        {"id": item_id, "type": "todo_list", "items": items},
        parent_uuid="p",
        event_uuid=_stable_uuid(thread, item_id),
    )
    return {"type": "agent_message", "data": data}


def _codex_update_plan(call_id: str, plan: list, explanation: str = "") -> dict:
    """Codex `update_plan` function_call run through `runner_codex`
    normalization (mapped to a TodoWrite tool_use), then wrapped as the
    agent_message the tailer feeds to apply_event. Each real update_plan
    call carries a DISTINCT call_id (unlike todo_list's stable item id),
    so it behaves like a normal TodoWrite tool_use."""
    import json as _json
    from runner_codex import _normalize_response_tool_call
    event, _ = _normalize_response_tool_call(
        {
            "type": "function_call",
            "name": "update_plan",
            "call_id": call_id,
            "arguments": _json.dumps({
                "plan": plan,
                **({"explanation": explanation} if explanation else {}),
            }),
        },
        parent_uuid="p",
    )
    return {"type": "agent_message", "data": event}


def _count_todowrite_render_nodes(msg: dict) -> int:
    """Count entries in msg.events carrying a TodoWrite tool_use —
    stable-uuid REPLACE keeps this at 1 across re-emissions."""
    n = 0
    for ev in (msg.get("events") or []):
        content = (
            (ev.get("data") or {}).get("message", {}).get("content")
            if isinstance(ev.get("data"), dict) else None
        )
        if not isinstance(content, list):
            continue
        if any(
            isinstance(b, dict) and b.get("type") == "tool_use"
            and b.get("name") == "TodoWrite"
            for b in content
        ):
            n += 1
    return n


def _apply(
    strategy,
    sid: str,
    msg: dict,
    ev: dict,
    source_is_provider_stream: bool,
) -> None:
    ctx = ApplyEventCtx(
        manager_sid_holder={"id": None}, workers_list=[],
        user_msg=None, root_id=sid,
    )
    strategy.apply_event(
        app_session_id=sid, msg=msg, event=ev, ctx=ctx,
        source_is_provider_stream=source_is_provider_stream,
    )
    session_event_extensions.drain_for_tests()


def _task_create(uuid: str, subject: str, activeForm: str | None = None,
                 description: str = "", tool_id: str = "tc_1") -> dict:
    """Native-mode agent_message with a TaskCreate tool_use block."""
    return {
        "type": "agent_message",
        "data": {
            "uuid": uuid,
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "TaskCreate",
                    "input": {
                        "subject": subject,
                        "description": description,
                        "activeForm": activeForm,
                    },
                }],
            },
        },
    }


def _task_update(uuid: str, task_id: str, status: str | None = None,
                 subject: str | None = None, activeForm: str | None = None,
                 tool_id: str = "tu_1") -> dict:
    """Native-mode agent_message with a TaskUpdate tool_use block."""
    return {
        "type": "agent_message",
        "data": {
            "uuid": uuid,
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "TaskUpdate",
                    "input": {
                        "taskId": task_id,
                        "status": status,
                        "subject": subject,
                        "activeForm": activeForm,
                    },
                }],
            },
        },
    }


def _task_create_result(uuid: str, tool_use_id: str, task_id: str,
                        result_uuid: str = "ru_1") -> dict:
    """User-role agent_message with a tool_result for a TaskCreate.

    Mirrors the enriched shape from claude_jsonl_enrich: the raw
    user message is wrapped as type=agent_message with role=user.
    """
    return {
        "type": "agent_message",
        "data": {
            "uuid": result_uuid,
            "type": "user",
            "message": {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": str(task_id),
                }],
            },
        },
    }


# ─── tests ───────────────────────────────────────────────────────

def test_claude_todowrite_first_call_sets_list() -> bool:
    """Claude's TodoWrite carries the entire todo list on every call.
    First call populates current_todos (UNION with empty = REPLACE)."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    todos = [
        {"content": "step 1", "status": "in_progress", "activeForm": "Doing 1"},
        {"content": "step 2", "status": "pending", "activeForm": "Doing 2"},
    ]
    _apply(strategy, sid, msg, _claude_todowrite_native("u1", todos), source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_todos") or []
    expected = [
        {"content": "step 1", "status": "in_progress", "activeForm": "Doing 1"},
        {"content": "step 2", "status": "pending", "activeForm": "Doing 2"},
    ]
    if got != expected:
        print(f"  expected {expected}, got {got}")
        return False
    return True


def test_claude_todowrite_union_keeps_completed_across_phases() -> bool:
    """Core UNION-merge scenario: Claude finishes phase 1 (A, B
    completed) and starts phase 2 (C, D). With REPLACE, A and B would
    be lost. With UNION, all four items persist."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    # Phase 1: A completed, B completed
    phase1 = [
        {"content": "Phase1: setup project", "status": "completed", "activeForm": "Setting up"},
        {"content": "Phase1: write tests", "status": "completed", "activeForm": "Writing tests"},
    ]
    _apply(strategy, sid, msg, _claude_todowrite_native("u1", phase1), source_is_provider_stream=True)
    # Phase 2: completely new items — Claude replaces its list
    phase2 = [
        {"content": "Phase2: refactor module", "status": "in_progress", "activeForm": "Refactoring"},
        {"content": "Phase2: update docs", "status": "pending", "activeForm": "Updating docs"},
    ]
    _apply(strategy, sid, msg, _claude_todowrite_native("u2", phase2), source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_todos") or []
    if len(got) != 4:
        print(f"  expected 4 items, got {len(got)}: {got}")
        return False
    contents = [t["content"] for t in got]
    if contents != [
        "Phase1: setup project", "Phase1: write tests",
        "Phase2: refactor module", "Phase2: update docs",
    ]:
        print(f"  wrong contents: {contents}")
        return False
    statuses = [t["status"] for t in got]
    if statuses != ["completed", "completed", "in_progress", "pending"]:
        print(f"  wrong statuses: {statuses}")
        return False
    return True


def test_claude_two_sequential_todowrites_union_merge() -> bool:
    """Sample-confirmed (real session data): TodoWrite #2 reposts the
    whole list with an updated status. UNION-merge updates A in place
    and appends B — items from #1 are never lost."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    first = [{"content": "A", "status": "in_progress", "activeForm": "doing A"}]
    second = [
        {"content": "A", "status": "completed", "activeForm": "doing A"},
        {"content": "B", "status": "in_progress", "activeForm": "doing B"},
    ]
    _apply(strategy, sid, msg, _claude_todowrite_native("u1", first), source_is_provider_stream=True)
    _apply(strategy, sid, msg, _claude_todowrite_native("u2", second), source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_todos") or []
    if got != [
        {"content": "A", "status": "completed", "activeForm": "doing A"},
        {"content": "B", "status": "in_progress", "activeForm": "doing B"},
    ]:
        print(f"  got {got}")
        return False
    return True


def test_gemini_single_update_topic_appends_in_progress() -> bool:
    """Gemini's single-topic call → one new in_progress item with
    `content=title`, `activeForm=summary`, `source_id=tool_id`."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    ev = _gemini_update_topic(
        "u1", "update_topic_1780096408174_0",
        title="Investigating Foo", summary="Looking into Foo logs",
    )
    _apply(strategy, sid, msg, ev, source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_todos") or []
    if got != [{
        "content": "Investigating Foo",
        "status": "in_progress",
        "activeForm": "Looking into Foo logs",
        "source_id": "update_topic_1780096408174_0",
    }]:
        print(f"  got {got}")
        return False
    return True


def test_gemini_three_sequential_prior_completed() -> bool:
    """Three sequential update_topic calls → first two become
    `completed`, third stays `in_progress`. Best-effort UX heuristic
    documented as project caveat."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    for i, (title, summary) in enumerate([
        ("topic 1", "doing 1"),
        ("topic 2", "doing 2"),
        ("topic 3", "doing 3"),
    ]):
        tid = f"update_topic_178000000{i}_0"
        _apply(
            strategy, sid, msg,
            _gemini_update_topic(f"u{i}", tid, title=title, summary=summary),
            source_is_provider_stream=True,
        )
    got = session_manager.get(sid).get("current_todos") or []
    statuses = [t["status"] for t in got]
    if statuses != ["completed", "completed", "in_progress"]:
        print(f"  statuses: {statuses}")
        return False
    titles = [t["content"] for t in got]
    if titles != ["topic 1", "topic 2", "topic 3"]:
        print(f"  titles: {titles}")
        return False
    return True


def test_gemini_same_source_id_same_content_noop() -> bool:
    """Replay safety: same tool_id replayed with same content does NOT
    grow the list (dedup-by-source_id)."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    ev = _gemini_update_topic(
        "u1", "update_topic_1780000000_0", title="T", summary="S",
    )
    _apply(strategy, sid, msg, ev, source_is_provider_stream=True)
    _apply(strategy, sid, msg, ev, source_is_provider_stream=False)  # recovery replay
    got = session_manager.get(sid).get("current_todos") or []
    if len(got) != 1:
        print(f"  expected len 1, got {len(got)}: {got}")
        return False
    return True


def test_gemini_same_source_id_mutated_content_replaces() -> bool:
    """If Gemini re-emits the same tool_id with updated input (in-place
    streaming mutation), the matching entry is REPLACED in place — the
    list length stays 1."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    tid = "update_topic_1780000000_0"
    _apply(
        strategy, sid, msg,
        _gemini_update_topic("u1", tid, title="early", summary="early s"),
        source_is_provider_stream=True,
    )
    _apply(
        strategy, sid, msg,
        # Same tool_id but the underlying agent_message uuid differs
        # (Gemini streaming sometimes mutates either; either should
        # work — but we use a new uuid here to model "next streamed
        # chunk for the same tool_use").
        _gemini_update_topic("u2", tid, title="final", summary="final s"),
        source_is_provider_stream=True,
    )
    got = session_manager.get(sid).get("current_todos") or []
    if len(got) != 1:
        print(f"  expected len 1, got {len(got)}")
        return False
    if got[0]["content"] != "final" or got[0]["activeForm"] != "final s":
        print(f"  expected mutated content, got {got[0]}")
        return False
    return True


def test_gemini_tool_id_pattern_mismatch_uses_content_hash() -> bool:
    """A tool_id NOT matching `update_topic_\\d+_\\d+` (e.g. the
    `runner_gemini._new_uuid()` fallback) falls back to a deterministic
    content-hash source_id. Two replays of the same input yield the
    same hash → dedup still works → no list growth."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    # Use a uuid-shaped tool_id that does NOT match the gemini pattern.
    ev1 = _gemini_update_topic(
        "u1", "9d1f-not-a-gemini-id", title="X", summary="Y",
    )
    ev2 = _gemini_update_topic(
        "u2", "different-uuid-but-same-input",
        title="X", summary="Y",
    )
    _apply(strategy, sid, msg, ev1, source_is_provider_stream=True)
    _apply(strategy, sid, msg, ev2, source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_todos") or []
    if len(got) != 1:
        print(f"  expected len 1 (content-hash dedup), got {len(got)}: {got}")
        return False
    # source_id is the content hash — 16 hex chars.
    sid_field = got[0].get("source_id") or ""
    if len(sid_field) != 16 or not all(c in "0123456789abcdef" for c in sid_field):
        print(f"  expected 16-hex content-hash source_id, got {sid_field!r}")
        return False
    return True


def test_extractor_skip_when_tool_id_missing() -> bool:
    """Empty/missing tool_use.id → extractor returns None (no
    undedupable item appended). Defensive: the runner SHOULD always
    supply an id, but if it doesn't, we'd diverge across replays."""
    normalized = {
        "type": "agent_message",
        "data": {
            "uuid": "u1",
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": "",
                    "name": "TodoWrite",
                    "input": {"title": "T", "summary": "S"},
                }],
            },
        },
    }
    if extract_todos_from_normalized(normalized, []) is not None:
        print("  expected None on empty id")
        return False
    return True


def test_extractor_skip_non_serializable_gemini_input() -> bool:
    """Defensive: a Gemini input with non-JSON-serializable values
    (e.g. bytes) bypasses content-hash fallback → return None, NOT
    crash."""
    normalized = {
        "type": "agent_message",
        "data": {
            "uuid": "u1",
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": "not-a-gemini-pattern-id",
                    "name": "TodoWrite",
                    "input": {"title": "T", "summary": b"\xff bytes"},
                }],
            },
        },
    }
    if extract_todos_from_normalized(normalized, []) is not None:
        print("  expected None on non-serializable input")
        return False
    return True


def test_extractor_purity_does_not_mutate_current() -> bool:
    """Locked invariant: extractor MUST NOT mutate `current` or its
    items. Callers pass shallow-copy snapshots and expect them intact."""
    current = [{
        "content": "old A", "status": "in_progress", "activeForm": "A",
        "source_id": "sid-old",
    }]
    snapshot = copy.deepcopy(current)
    ev = _gemini_update_topic(
        "u1", "update_topic_1780000000_0", title="new", summary="ns",
    )
    extract_todos_from_normalized(ev, current)
    if current != snapshot:
        print(f"  extractor mutated current: {current} != {snapshot}")
        return False
    return True


def test_worker_event_todowrite_does_not_touch_session_todos() -> bool:
    """`worker_event`-wrapped TodoWrite belongs to the worker panel's
    own events, not the session-level current_todos. The
    worker_event branch early-returns in apply_event BEFORE the
    extractor hook fires — confirm here."""
    sid, msg = _mk_session("manager")
    strategy = get_strategy("manager")
    inner_todos = [{"content": "W", "status": "pending", "activeForm": "W"}]
    worker_ev = {
        "type": "worker_event",
        "data": {
            "delegation_id": "deleg-1",
            "event": _claude_todowrite_native("uw", inner_todos),
        },
    }
    _apply(strategy, sid, msg, worker_ev, source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_todos") or []
    if got:
        print(f"  expected empty current_todos, got {got}")
        return False
    return True


def test_interleaved_manager_and_agent_message_accumulates() -> bool:
    """Interleaved manager_event-wrapped and raw agent_message
    TodoWrites in the same session both flow through apply_event —
    UNION-merge keeps both items regardless of stream."""
    sid, msg = _mk_session("manager")
    strategy = get_strategy("manager")
    first = [{"content": "from manager", "status": "in_progress", "activeForm": "m"}]
    second = [{"content": "from raw", "status": "in_progress", "activeForm": "r"}]
    _apply(strategy, sid, msg,
           _claude_todowrite_manager("u1", first, "tu_m"), source_is_provider_stream=True)
    _apply(strategy, sid, msg,
           _claude_todowrite_native("u2", second, "tu_n"), source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_todos") or []
    if len(got) != 2:
        print(f"  expected 2 items, got {len(got)}: {got}")
        return False
    contents = [t["content"] for t in got]
    if contents != ["from manager", "from raw"]:
        print(f"  wrong contents: {contents}")
        return False
    return True


def test_convergence_invariant_live_equals_recovery() -> bool:
    """CLAUDE.md convergence invariant: the same event sequence
    applied via source_is_provider_stream=True and via source_is_provider_stream=False produces byte-equal
    `current_todos`. Locks scenario 1 == scenario 3 for the new
    derived field."""
    seq_claude = [
        ("u1", [{"content": "A", "status": "in_progress", "activeForm": "a"}]),
        ("u2", [
            {"content": "A", "status": "completed", "activeForm": "a"},
            {"content": "B", "status": "in_progress", "activeForm": "b"},
        ]),
    ]
    # Live path.
    sid_live, msg_live = _mk_session("native")
    strategy_live = get_strategy("native")
    for uid, todos in seq_claude:
        _apply(strategy_live, sid_live, msg_live,
               _claude_todowrite_native(uid, todos, tool_id=f"tu_{uid}"),
               source_is_provider_stream=True)
    live_state = session_manager.get(sid_live).get("current_todos") or []

    # Recovery path: same events, source_is_provider_stream=False (events.jsonl replay).
    sid_rec, msg_rec = _mk_session("native")
    strategy_rec = get_strategy("native")
    for uid, todos in seq_claude:
        _apply(strategy_rec, sid_rec, msg_rec,
               _claude_todowrite_native(uid, todos, tool_id=f"tu_{uid}"),
               source_is_provider_stream=False)
    recovery_state = session_manager.get(sid_rec).get("current_todos") or []

    if live_state != recovery_state:
        print(f"  divergence: source_is_provider_stream={live_state} recovery={recovery_state}")
        return False
    return True


def test_convergence_invariant_gemini_replay() -> bool:
    """Gemini path of the convergence invariant: same update_topic
    sequence via source_is_provider_stream=True vs source_is_provider_stream=False → byte-equal current_todos.
    Pins replay-stability of `source_id` dedup (both gemini-pattern
    and content-hash branches)."""
    seq = [
        ("update_topic_1780000001_0", "topic 1", "doing 1"),
        ("update_topic_1780000002_0", "topic 2", "doing 2"),
        # Mismatched id forces content-hash branch.
        ("free-form-uuid-shape", "topic 3", "doing 3"),
    ]

    def run(source_is_provider_stream: bool) -> list:
        sid, msg = _mk_session("native")
        strategy = get_strategy("native")
        for i, (tid, title, summary) in enumerate(seq):
            _apply(strategy, sid, msg,
                   _gemini_update_topic(f"u{i}", tid, title=title, summary=summary),
                   source_is_provider_stream=source_is_provider_stream)
        return session_manager.get(sid).get("current_todos") or []

    if run(source_is_provider_stream=True) != run(source_is_provider_stream=False):
        print("  Gemini live != Gemini recovery")
        return False
    return True


def test_equality_skip_suppresses_redundant_fires() -> bool:
    """The hook exits early when the extracted list equals the current state — no `todos_updated`
    fire on a no-op.

    Uses DISTINCT event uuids that produce IDENTICAL extractor
    output. If both events shared a uuid, the dedup gate at
    `base.py` would short-circuit BEFORE reaching the hook —
    exercising the gate instead of the hook's precheck. Two
    different uuids carrying the same TodoWrite list isolate the
    precheck path.
    """
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    fires = []
    def listener(s_id, change):
        if change.get("kind") == "todos_updated":
            fires.append(change)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        session_manager.add_listener(listener)
    try:
        todos = [{"content": "X", "status": "pending", "activeForm": "x"}]
        _apply(
            strategy, sid, msg,
            _claude_todowrite_native("u1", todos, tool_id="tu1"),
            source_is_provider_stream=True,
        )
        first_count = len(fires)
        # Distinct uuid + tool_id → bypasses the msg.events dedup
        # gate. Same todos list → extractor returns equal list →
        # hook's precheck skips the fire.
        _apply(
            strategy, sid, msg,
            _claude_todowrite_native("u2", todos, tool_id="tu2"),
            source_is_provider_stream=True,
        )
        if len(fires) != first_count:
            print(
                f"  expected hook precheck to suppress fire on equal "
                f"list, fires went {first_count} -> {len(fires)}",
            )
            return False
    finally:
        session_manager._listeners.remove(listener)
    return True


def test_claude_in_progress_not_demoted_by_gemini_delta() -> bool:
    """Hostile review hole #2: when Gemini `update_topic` arrives
    after a Claude TodoWrite that left items `in_progress`, those
    Claude items (no `source_id`) MUST NOT be silently demoted to
    `completed`. Only prior Gemini-owned (source_id-bearing) items
    rotate to completed on a new delta.
    """
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    claude_todos = [
        {"content": "claude A", "status": "in_progress", "activeForm": "a"},
        {"content": "claude B", "status": "pending", "activeForm": "b"},
    ]
    _apply(
        strategy, sid, msg,
        _claude_todowrite_native("u1", claude_todos, tool_id="tu_claude"),
        source_is_provider_stream=True,
    )
    _apply(
        strategy, sid, msg,
        _gemini_update_topic(
            "u2", "update_topic_1780000099_0",
            title="gemini topic", summary="doing it",
        ),
        source_is_provider_stream=True,
    )
    got = session_manager.get(sid).get("current_todos") or []
    # Expect 3 items: claude A (still in_progress), claude B (pending),
    # gemini topic (in_progress). Claude A must NOT be flipped.
    if len(got) != 3:
        print(f"  expected 3 items, got {len(got)}: {got}")
        return False
    a = next((t for t in got if t["content"] == "claude A"), None)
    if a is None or a["status"] != "in_progress":
        print(f"  claude A demoted: {a}")
        return False
    g = next((t for t in got if t.get("source_id", "").startswith("update_topic_")), None)
    if g is None or g["status"] != "in_progress":
        print(f"  gemini topic missing/wrong status: {g}")
        return False
    return True


def test_two_gemini_deltas_prior_completes_only_self() -> bool:
    """Companion to the demotion test: two sequential Gemini deltas
    (no Claude in between) STILL rotate the prior Gemini item to
    `completed`. The scope is "Gemini-owned only", not "no rotation".
    """
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    _apply(strategy, sid, msg, _gemini_update_topic(
        "u1", "update_topic_1780000100_0", title="g1", summary="s1",
    ), source_is_provider_stream=True)
    _apply(strategy, sid, msg, _gemini_update_topic(
        "u2", "update_topic_1780000101_0", title="g2", summary="s2",
    ), source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_todos") or []
    statuses = [(t["content"], t["status"]) for t in got]
    if statuses != [("g1", "completed"), ("g2", "in_progress")]:
        print(f"  unexpected statuses: {statuses}")
        return False
    return True


def test_fork_derives_current_todos_from_copied_messages() -> bool:
    """Forks re-derive `current_todos` from the COPIED messages'
    events through the extractor — NOT inherit the parent's running
    state (which may reflect events past the fork point) and NOT
    leave the fork empty (which would lose state on every fork).
    """
    messages = [
        {
            "id": "m1", "role": "assistant", "seq": 0,
            "events": [
                _claude_todowrite_native("uA", [
                    {"content": "A", "status": "in_progress", "activeForm": "a"},
                    {"content": "B", "status": "pending", "activeForm": "b"},
                ], tool_id="tuA"),
                _claude_todowrite_native("uB", [
                    {"content": "A", "status": "completed", "activeForm": "a"},
                    {"content": "B", "status": "in_progress", "activeForm": "b"},
                ], tool_id="tuB"),
            ],
        },
    ]
    derived = derive_current_todos(messages)
    expected = [
        {"content": "A", "status": "completed", "activeForm": "a"},
        {"content": "B", "status": "in_progress", "activeForm": "b"},
    ]
    if derived != expected:
        print(f"  expected {expected}, got {derived}")
        return False
    return True


def test_fork_skip_worker_panel_events() -> bool:
    """`derive_current_todos` walks the flat `msg.events` ONLY —
    worker panel events under `msg.workers[*].events` are intentionally
    NOT walked (matches the apply_event worker_event early-return)."""
    messages = [{
        "id": "m1", "role": "assistant", "seq": 0,
        "events": [],
        "workers": [{
            "delegation_id": "d1",
            "events": [_claude_todowrite_native("wu", [
                {"content": "worker", "status": "in_progress", "activeForm": "w"},
            ], tool_id="tu_w")],
        }],
    }]
    derived = derive_current_todos(messages)
    if derived != []:
        print(f"  expected empty, got {derived}")
        return False
    return True


def test_concurrent_gemini_deltas_no_lost_update() -> bool:
    """Hostile review hole #3 (TOCTOU): two concurrent Gemini
    `update_topic` events on the same session must NOT lose either
    one. The session-event projection closes the snapshot→set race window. Simulated by interleaving
    apply_event calls from two threads on the same sid.
    """
    import threading

    sid, msg = _mk_session("native")
    strategy = get_strategy("native")

    barrier = threading.Barrier(2)
    errors: list = []

    def fire(uid: str, tid: str, title: str) -> None:
        try:
            barrier.wait(timeout=2.0)
            _apply(strategy, sid, msg,
                   _gemini_update_topic(uid, tid, title=title, summary=title),
                   source_is_provider_stream=True)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=fire, args=(
        "u1", "update_topic_1780000200_0", "topic 1",
    ))
    t2 = threading.Thread(target=fire, args=(
        "u2", "update_topic_1780000201_0", "topic 2",
    ))
    t1.start(); t2.start()
    t1.join(); t2.join()

    if errors:
        print(f"  thread errors: {errors}")
        return False

    got = session_manager.get(sid).get("current_todos") or []
    titles = sorted(t["content"] for t in got)
    if titles != ["topic 1", "topic 2"]:
        print(f"  expected both topics, got {titles} (lost update)")
        return False
    # Exactly one in_progress (whichever lost the race) — the second
    # delta marks the first's prior in_progress as completed.
    in_prog = [t for t in got if t["status"] == "in_progress"]
    if len(in_prog) != 1:
        print(f"  expected 1 in_progress, got {len(in_prog)}: {got}")
        return False
    return True


def test_hydration_loads_current_todos_from_events_jsonl() -> bool:
    """End-to-end hydration integration: build a session via live
    ingest (writes to events.jsonl AND populates in-memory msg.events
    + current_todos). Then drop the session out of the in-memory cache
    AND clear current_todos on disk. Re-load via session_manager.get —
    the on-disk JSON path runs through `_load_root` →
    `hydrate_msg_events_from_jsonl` → `apply_event(source_is_provider_stream=False)` for
    each event → the todos hook fires → `current_todos` repopulates
    in memory. Locks the "no migration walk needed" plan decision:
    lazy hydration via apply_event during the load funnel.
    """
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    todos = [{"content": "Hyd", "status": "in_progress", "activeForm": "h"}]
    _apply(strategy, sid, msg,
           _claude_todowrite_native("uH", todos, tool_id="tu_h"),
           source_is_provider_stream=True)

    # Sanity: live path populated current_todos.
    if (session_manager.get(sid).get("current_todos") or []) != [
        {"content": "Hyd", "status": "in_progress", "activeForm": "h"},
    ]:
        print("  live path failed to populate baseline")
        return False

    # Force on-disk current_todos to empty to simulate an existing
    # session that pre-dates this feature. Then evict in-memory state
    # so the next get() re-loads from disk.
    import session_store
    from session_store import write_session_full, get_session
    root_id = session_store._resolve_root_id(sid)
    root = get_session(root_id)
    root["current_todos"] = []
    write_session_full(root, bump_updated_at=False)

    # Evict from session_manager's in-memory cache so the next get()
    # triggers `_load_root` (which runs hydration).
    rid = session_manager._root_id_for(sid)
    with session_manager._lock_for_root(rid):
        session_manager._roots.pop(rid, None)
        session_manager._event_hydrated_roots.discard(rid)

    # Re-load: hits the disk path → hydrate replays events.jsonl
    # through apply_event(source_is_provider_stream=False) → todos hook fires → field
    # repopulates in memory.
    reloaded = session_manager.get(sid) or {}
    got = reloaded.get("current_todos") or []
    if got != [{"content": "Hyd", "status": "in_progress", "activeForm": "h"}]:
        print(f"  hydration failed to repopulate current_todos: {got}")
        return False
    return True


def test_gemini_reemission_preserves_completed_status() -> bool:
    """Real-data regression: Gemini sometimes re-emits a previously-
    completed topic's tool_id later in the session. The replace-in-
    place branch must NOT downgrade `completed` back to `in_progress`.
    Verified against ~/.better-claude/sessions/33b3991a where 7 unique
    tool_ids were each re-emitted, leading to all-in_progress
    rendering until this fix.
    """
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    # First emission: A becomes in_progress.
    _apply(strategy, sid, msg, _gemini_update_topic(
        "u1", "update_topic_1780000300_0", title="A", summary="a",
    ), source_is_provider_stream=True)
    # Second emission: B → A flips to completed, B in_progress.
    _apply(strategy, sid, msg, _gemini_update_topic(
        "u2", "update_topic_1780000301_0", title="B", summary="b",
    ), source_is_provider_stream=True)
    # Re-emission of A (same tool_id, possibly mutated content) —
    # must NOT downgrade A's completed status.
    _apply(strategy, sid, msg, _gemini_update_topic(
        "u3", "update_topic_1780000300_0",
        title="A revised", summary="a revised",
    ), source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_todos") or []
    if len(got) != 2:
        print(f"  expected 2 items, got {len(got)}: {got}")
        return False
    a = next((t for t in got if t["source_id"] == "update_topic_1780000300_0"), None)
    if a is None or a["status"] != "completed":
        print(f"  A downgraded after re-emission: {a}")
        return False
    # Content/activeForm updated despite preserving status — that's
    # how Gemini streaming mutations are meant to flow through.
    if a["content"] != "A revised":
        print(f"  A's content not updated: {a}")
        return False
    return True


def test_load_derives_current_todos_from_orphan_rows() -> bool:
    """The hydrate fast path (render_tree_hydrate.py:76-91) skips
    events.jsonl when msg.events is already populated AND no streaming
    msgs exist — so msg_id=None ORPHAN rows never reach apply_event.
    Verified against real session data
    (~/.better-claude/sessions/12eb332c... has 4 orphan TodoWrites
    that the hook never fired for).

    `_load_root._derive_current_todos_from_events_jsonl` reads
    events.jsonl directly and walks the extractor over EVERY row
    (named + orphan) in seq order — the only authoritative backfill
    for cross-provider current_todos. Idempotent (same rows → same
    list), safe to call on every cold load.
    """
    from event_ingester import event_ingester

    sid, msg = _mk_session("native")
    todos = [{"content": "Orph", "status": "in_progress", "activeForm": "o"}]
    # Ingest a TodoWrite row WITHOUT a msg_id — mirrors what the
    # primary CLI tailer writes when it fires after the orchestrator
    # already finalized the turn.
    event = _claude_todowrite_native("u-orph", todos, tool_id="tu_orph")
    event_ingester.ingest(
        sid, sid=sid,
        event_type="agent_message", data=event["data"],
        source="test_orphan", run_id=None, msg_id=None,
    )

    # Wipe in-memory current_todos so we're testing the derive path,
    # not a leftover from the apply_event hook firing earlier.
    session_manager.set_current_todos(sid, [])

    # Evict the in-memory root so the next get() runs _load_root
    # (which calls the derive helper).
    rid = session_manager._root_id_for(sid)
    with session_manager._lock_for_root(rid):
        session_manager._roots.pop(rid, None)
        session_manager._event_hydrated_roots.discard(rid)

    with session_manager._lock_for_root(rid):
        sess = session_manager._load_root(sid) or {}
        got = sess.get("current_todos") or []
    if got != todos:
        print(f"  expected orphan-derived todos, got {got}")
        return False
    return True


def test_cli_prompt_open_todos_only() -> bool:
    """The helper injects only unfinished todos and otherwise no-ops."""
    from turn_helpers import _append_todo_reminder

    user_text = "do the thing"

    if _append_todo_reminder(user_text, {"current_todos": []}) != user_text:
        print("  empty todo list changed prompt")
        return False

    sess_with = {"current_todos": [
        {"content": "X <unsafe>", "status": "in_progress"},
        {"content": "Still pending", "status": "pending"},
        {"content": "Already done", "status": "completed"},
    ]}
    out = _append_todo_reminder(user_text, sess_with)
    if not out.startswith(user_text):
        print(f"  user text not preserved: {out!r}")
        return False
    if "<bc-todo-reminder>" not in out or "</bc-todo-reminder>" not in out:
        print(f"  unfinished todo tags missing: {out!r}")
        return False
    if "X &lt;unsafe&gt;" not in out or "Still pending" not in out:
        print(f"  unfinished todo content missing or unescaped: {out!r}")
        return False
    if "Already done" in out:
        print(f"  completed todo leaked into reminder: {out!r}")
        return False

    all_done = {"current_todos": [
        {"content": "Already done", "status": "completed"},
    ]}
    if _append_todo_reminder(user_text, all_done) != user_text:
        print("  all-completed list changed prompt")
        return False

    return True


def test_all_tasks_done_marker_completes_todos_and_suppresses_reminder() -> bool:
    sid, msg = _mk_session("native")
    session_manager.set_current_todos(sid, [
        {"content": "Check project orientation", "status": "in_progress", "activeForm": None},
        {"content": "Draft concise implementation plan", "status": "pending", "activeForm": None},
    ])

    import file_ref_resolver
    file_ref_resolver.set_tag_rules([
        {
            "tag": "ALL_TASKS__DONE",
            "strip_wrapper": True,
            "_extension_id": "ofek-dev.user-attention",
            "marker": {"color": "#2563eb", "tooltip": "All tasks done"},
        }
    ])

    strategy = get_strategy("native")
    done_event = {
        "type": "agent_message",
        "data": {
            "uuid": "all_done_msg",
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "<ALL_TASKS__DONE>All requested work is done.</ALL_TASKS__DONE>",
                    }
                ],
            },
        },
    }

    import session_local_projection
    projected = session_local_projection.project_event_fields(
        done_event,
        current_todos=[
            {"content": "Cold-load stale", "status": "in_progress", "activeForm": None},
        ],
        current_tasks=[],
    )
    if [item.get("status") for item in projected.get("current_todos") or []] != ["completed"]:
        print(f"  cold-load projection did not complete todos: {projected}")
        return False

    _apply(strategy, sid, msg, done_event, source_is_provider_stream=True)

    got = session_manager.get(sid).get("current_todos") or []
    if [item.get("status") for item in got] != ["completed", "completed"]:
        print(f"  expected marker to complete todos, got {got}")
        return False

    from turn_helpers import _append_todo_reminder
    if _append_todo_reminder("next prompt", session_manager.get(sid)) != "next prompt":
        print("  completed marker did not suppress next todo reminder")
        return False
    return True


def test_run_turn_gate_covers_every_user_prompt() -> bool:
    """Runtime gate test: exercise the EXACT boolean expression at the
    `run_turn` inject site by extracting it from the source via AST.
    Every non-empty user prompt must pass through the helper regardless
    of frontend/CLI transport or working mode. Internal and empty turns
    must skip it.
    """
    import ast
    import inspect
    import turn_manager

    src = inspect.getsource(turn_manager)
    tree = ast.parse(src)

    gate_test_src: str | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "TurnManager":
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name != "run_turn":
                continue
            for sub in ast.walk(item):
                if not isinstance(sub, ast.If):
                    continue
                # Find the If whose body calls _append_todo_reminder.
                for body_stmt in sub.body:
                    for inner in ast.walk(body_stmt):
                        if (
                            isinstance(inner, ast.Call)
                            and isinstance(inner.func, ast.Name)
                            and inner.func.id == "_append_todo_reminder"
                        ):
                            gate_test_src = ast.unparse(sub.test)
                            break
                    if gate_test_src:
                        break
                if gate_test_src:
                    break
    if not gate_test_src:
        print("  could not locate the gate If around _append_todo_reminder")
        return False

    # Evaluate the gate expression for each case.
    cases = [
        # (user_initiated, prompt, expected_to_inject, label)
        (True,  "hi",    True,  "user prompt"),
        (False, "hi",    False, "internal turn"),
        (True,  "",      False, "empty prompt"),
        (True,  "  \n",  False, "whitespace prompt"),
    ]
    for ui, p, expected, label in cases:
        ns = {
            "user_initiated": ui,
            "prompt": p,
        }
        actual = bool(eval(gate_test_src, {}, ns))
        if actual != expected:
            print(
                f"  gate({label}): expected {expected}, got {actual} — "
                f"gate expression at injection site: {gate_test_src}"
            )
            return False
    return True


def test_dispatch_supervisor_branch_passes_user_initiated() -> bool:
    """`handle_prompt`'s `send_target=='supervisor'` branch calls
    `run_turn` directly (bypassing the native/manager handle_turn
    wrappers that set `user_initiated=True`). It MUST pass
    `user_initiated=True` itself — otherwise the gate at run_turn's
    nudge site silently skips the supervisor-target path. Same class
    of bug as the original manager-mode drop.

    AST walk: find the `if send_target == "supervisor"` branch in
    `handle_prompt` and assert its `run_turn` call has the keyword.
    """
    import ast
    import inspect
    import orchestrator

    src = inspect.getsource(orchestrator)
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "Coordinator":
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name != "handle_prompt":
                continue
            for sub in ast.walk(item):
                # Match the supervisor If gate.
                if not isinstance(sub, ast.If):
                    continue
                txt = ast.unparse(sub.test)
                if 'send_target' not in txt or 'supervisor' not in txt:
                    continue
                # Find the run_turn call inside its body.
                for body_stmt in sub.body:
                    for inner in ast.walk(body_stmt):
                        if (
                            isinstance(inner, ast.Call)
                            and isinstance(inner.func, ast.Attribute)
                            and inner.func.attr == "run_turn"
                        ):
                            kws = {kw.arg: kw for kw in inner.keywords}
                            ui_kw = kws.get("user_initiated")
                            if ui_kw is None:
                                print(
                                    "  supervisor branch's run_turn call "
                                    "missing `user_initiated=` keyword — "
                                    "the nudge is silently skipped for "
                                    "every direct-supervisor user prompt."
                                )
                                return False
                            # Must be literal True.
                            if not (
                                isinstance(ui_kw.value, ast.Constant)
                                and ui_kw.value.value is True
                            ):
                                print(
                                    f"  supervisor branch passes "
                                    f"user_initiated={ast.unparse(ui_kw.value)!r}; "
                                    f"expected literal True"
                                )
                                return False
                            found = True
    if not found:
        print("  could not locate the supervisor branch / run_turn call")
        return False
    return True


def test_run_turn_actually_calls_append_todo_reminder() -> bool:
    """Structural lock: `_append_todo_reminder` MUST be called from
    inside `TurnManager.run_turn`. Without this assertion,
    deleting the call line at the inject site would silently disable
    the entire feature and only the behavioral gate test below would
    still pass (it tests the gate expression, not the call).

    Walks the AST of `turn_manager.py` to find the `run_turn` method
    and assert that the helper is invoked from inside its body.
    Catches both "someone removed the call" and "someone moved it
    out of run_turn" regressions.
    """
    import ast
    import inspect
    import turn_manager

    src = inspect.getsource(turn_manager)
    tree = ast.parse(src)

    run_turn_calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "TurnManager":
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name != "run_turn":
                continue
            for sub in ast.walk(item):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                    run_turn_calls.append(sub.func.id)
    if "_append_todo_reminder" not in run_turn_calls:
        print(
            "  TurnManager.run_turn no longer calls "
            "_append_todo_reminder — the open-todo reminder feature is "
            "silently disabled."
        )
        return False
    return True


def test_get_current_todos_snapshot_returns_copy() -> bool:
    """The snapshot helper returns a shallow copy — caller mutations
    to the returned list MUST NOT leak back into the session record.
    Required by the apply_event hook contract (it passes the snapshot
    straight to the extractor, which builds fresh dicts on top)."""
    sid, _msg = _mk_session("native")
    session_manager.set_current_todos(sid, [
        {"content": "X", "status": "pending", "activeForm": "x"},
    ])
    snap = session_manager.get_current_todos_snapshot(sid)
    snap.append({"content": "Y", "status": "pending", "activeForm": "y"})
    fresh = session_manager.get(sid).get("current_todos") or []
    if len(fresh) != 1:
        print(f"  snapshot append leaked into session: {fresh}")
        return False
    return True


# ─── Codex todo_list tests ───────────────────────────────────────

def test_codex_todo_list_first_incomplete_is_in_progress() -> bool:
    """Codex `todo_list` (full snapshot, binary `completed`) maps to a
    Claude TodoWrite REPLACE. The FIRST not-completed entry surfaces as
    `in_progress` (Codex's stream lacks in_progress); the rest pending."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    items = [
        {"text": "Create hello.py", "completed": False},
        {"text": "Verify the file", "completed": False},
        {"text": "Report completion", "completed": False},
    ]
    _apply(strategy, sid, msg, _codex_todo_list("item_0", items), source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_todos") or []
    statuses = [(t["content"], t["status"]) for t in got]
    if statuses != [
        ("Create hello.py", "in_progress"),
        ("Verify the file", "pending"),
        ("Report completion", "pending"),
    ]:
        print(f"  got {statuses}")
        return False
    return True


def test_codex_todo_list_completed_then_first_incomplete() -> bool:
    """A completed leading entry stays completed; the first NOT-completed
    entry after it becomes in_progress, remainder pending."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    items = [
        {"text": "A", "completed": True},
        {"text": "B", "completed": False},
        {"text": "C", "completed": False},
    ]
    _apply(strategy, sid, msg, _codex_todo_list("item_0", items), source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_todos") or []
    statuses = [t["status"] for t in got]
    if statuses != ["completed", "in_progress", "pending"]:
        print(f"  got {statuses}")
        return False
    return True


def test_codex_todo_list_all_completed_no_in_progress() -> bool:
    """When every entry is completed, none is forced to in_progress."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    items = [
        {"text": "A", "completed": True},
        {"text": "B", "completed": True},
    ]
    _apply(strategy, sid, msg, _codex_todo_list("item_0", items), source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_todos") or []
    statuses = [t["status"] for t in got]
    if statuses != ["completed", "completed"]:
        print(f"  got {statuses}")
        return False
    return True


def test_codex_todo_list_stable_uuid_replaces_in_place() -> bool:
    """Codex re-emits the SAME todo_list item (stable id) across
    started→updated→completed. The stable per-(thread,id) uuid makes
    apply_event REPLACE the render node: current_todos reflects only the
    LATEST snapshot AND msg.events holds exactly ONE TodoWrite node."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    started = [
        {"text": "A", "completed": False},
        {"text": "B", "completed": False},
    ]
    completed = [
        {"text": "A", "completed": True},
        {"text": "B", "completed": False},
    ]
    _apply(strategy, sid, msg, _codex_todo_list("item_0", started), source_is_provider_stream=True)
    _apply(strategy, sid, msg, _codex_todo_list("item_0", completed), source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_todos") or []
    statuses = [t["status"] for t in got]
    if statuses != ["completed", "in_progress"]:
        print(f"  current_todos statuses: {statuses}")
        return False
    nodes = _count_todowrite_render_nodes(msg)
    if nodes != 1:
        print(f"  expected 1 todo render node (stable uuid), got {nodes}")
        return False
    return True


def test_codex_distinct_items_each_render() -> bool:
    """Two DIFFERENT codex item ids → distinct stable uuids → two
    separate render nodes (not collapsed)."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    _apply(strategy, sid, msg, _codex_todo_list("item_0", [
        {"text": "A", "completed": False},
    ]), source_is_provider_stream=True)
    _apply(strategy, sid, msg, _codex_todo_list("item_1", [
        {"text": "B", "completed": False},
    ]), source_is_provider_stream=True)
    nodes = _count_todowrite_render_nodes(msg)
    if nodes != 2:
        print(f"  expected 2 distinct render nodes, got {nodes}")
        return False
    return True


def test_codex_todo_list_convergence_live_equals_recovery() -> bool:
    """CLAUDE.md convergence invariant for the Codex path: the same
    todo_list emission sequence via source_is_provider_stream=True and source_is_provider_stream=False yields
    byte-equal current_todos (stable uuid + REPLACE are replay-safe)."""
    seq = [
        ("item_0", [
            {"text": "A", "completed": False},
            {"text": "B", "completed": False},
        ]),
        ("item_0", [
            {"text": "A", "completed": True},
            {"text": "B", "completed": False},
        ]),
    ]

    def run(source_is_provider_stream: bool) -> list:
        sid, msg = _mk_session("native")
        strategy = get_strategy("native")
        for item_id, items in seq:
            _apply(
                strategy, sid, msg, _codex_todo_list(item_id, items),
                source_is_provider_stream=source_is_provider_stream,
            )
        return session_manager.get(sid).get("current_todos") or []

    if run(source_is_provider_stream=True) != run(source_is_provider_stream=False):
        print("  Codex live != recovery")
        return False
    return True


# ─── Codex update_plan tests ─────────────────────────────────────


def test_codex_update_plan_maps_to_todos() -> bool:
    """Codex `update_plan` (`plan: [{step,status}]`) is normalized to a
    TodoWrite tool_use and reconstructed as current_todos. Status passes
    through — Codex shares TodoWrite's pending/in_progress/completed vocab."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    plan = [
        {"step": "Inspect runner input contract", "status": "in_progress"},
        {"step": "Create isolated A/B runner probe", "status": "pending"},
    ]
    _apply(strategy, sid, msg, _codex_update_plan("call_1", plan, "why"),
           source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_todos") or []
    statuses = [(t["content"], t["status"]) for t in got]
    if statuses != [
        ("Inspect runner input contract", "in_progress"),
        ("Create isolated A/B runner probe", "pending"),
    ]:
        print(f"  got {statuses}")
        return False
    return True


def test_codex_update_plan_status_progression_union_merge() -> bool:
    """Two sequential update_plan calls (same plan, statuses advancing)
    UNION-merge by content: the in_progress step → completed, the next
    → in_progress. Accumulates like Claude TodoWrite, no items lost."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    first = [
        {"step": "A", "status": "in_progress"},
        {"step": "B", "status": "pending"},
    ]
    second = [
        {"step": "A", "status": "completed"},
        {"step": "B", "status": "in_progress"},
    ]
    _apply(strategy, sid, msg, _codex_update_plan("call_1", first),
           source_is_provider_stream=True)
    _apply(strategy, sid, msg, _codex_update_plan("call_2", second),
           source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_todos") or []
    statuses = [(t["content"], t["status"]) for t in got]
    if statuses != [("A", "completed"), ("B", "in_progress")]:
        print(f"  got {statuses}")
        return False
    return True


def test_codex_update_plan_convergence_live_equals_recovery() -> bool:
    """Convergence invariant for the Codex update_plan path: the same
    emission sequence via source_is_provider_stream=True and False yields
    byte-equal current_todos (union-merge by content is replay-safe)."""
    seq = [
        ("call_1", [
            {"step": "A", "status": "in_progress"},
            {"step": "B", "status": "pending"},
        ]),
        ("call_2", [
            {"step": "A", "status": "completed"},
            {"step": "B", "status": "in_progress"},
        ]),
    ]

    def run(source_is_provider_stream: bool) -> list:
        sid, msg = _mk_session("native")
        strategy = get_strategy("native")
        for call_id, plan in seq:
            _apply(strategy, sid, msg, _codex_update_plan(call_id, plan),
                   source_is_provider_stream=source_is_provider_stream)
        return session_manager.get(sid).get("current_todos") or []

    def projection(todos):
        return [(t["content"], t["status"]) for t in todos]

    expected = [("A", "completed"), ("B", "in_progress")]
    live = projection(run(source_is_provider_stream=True))
    recovery = projection(run(source_is_provider_stream=False))
    if live != expected or recovery != expected:
        print(f"  expected {expected}\n  live={live}\n  recovery={recovery}")
        return False
    return True


# ─── TaskCreate / TaskUpdate tests ───────────────────────────────

def test_task_create_adds_pending_item() -> bool:
    """TaskCreate adds a new item with content=subject, status=pending."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    _apply(strategy, sid, msg,
           _task_create("u1", "Fix login bug", activeForm="Fixing login"), source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_tasks") or []
    if len(got) != 1:
        print(f"  expected 1 item, got {len(got)}: {got}")
        return False
    if got[0]["content"] != "Fix login bug":
        print(f"  wrong content: {got[0]['content']}")
        return False
    if got[0]["status"] != "pending":
        print(f"  wrong status: {got[0]['status']}")
        return False
    if got[0]["activeForm"] != "Fixing login":
        print(f"  wrong activeForm: {got[0]['activeForm']}")
        return False
    return True


def test_task_create_multiple_accumulates() -> bool:
    """Multiple TaskCreate calls accumulate items."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    _apply(strategy, sid, msg,
           _task_create("u1", "Task A", tool_id="tc1"), source_is_provider_stream=True)
    _apply(strategy, sid, msg,
           _task_create("u2", "Task B", tool_id="tc2"), source_is_provider_stream=True)
    _apply(strategy, sid, msg,
           _task_create("u3", "Task C", tool_id="tc3"), source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_tasks") or []
    if len(got) != 3:
        print(f"  expected 3 items, got {len(got)}: {got}")
        return False
    contents = [t["content"] for t in got]
    if contents != ["Task A", "Task B", "Task C"]:
        print(f"  wrong contents: {contents}")
        return False
    return True


def test_task_create_dedup_on_replay() -> bool:
    """Replaying the same TaskCreate doesn't duplicate items."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    ev = _task_create("u1", "Unique task", tool_id="tc1")
    _apply(strategy, sid, msg, ev, source_is_provider_stream=True)
    _apply(strategy, sid, msg, ev, source_is_provider_stream=False)  # recovery replay
    got = session_manager.get(sid).get("current_tasks") or []
    if len(got) != 1:
        print(f"  expected 1 item after replay, got {len(got)}: {got}")
        return False
    return True


def test_task_update_status_heuristic_pending_to_in_progress() -> bool:
    """TaskUpdate(status=in_progress) advances the first pending item."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    _apply(strategy, sid, msg,
           _task_create("u1", "Task A", tool_id="tc1"), source_is_provider_stream=True)
    _apply(strategy, sid, msg,
           _task_create("u2", "Task B", tool_id="tc2"), source_is_provider_stream=True)
    _apply(strategy, sid, msg,
           _task_update("u3", "1", status="in_progress", tool_id="tu1"), source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_tasks") or []
    statuses = [t["status"] for t in got]
    if statuses != ["in_progress", "pending"]:
        print(f"  wrong statuses: {statuses}")
        return False
    return True


def test_task_update_status_heuristic_to_completed() -> bool:
    """TaskUpdate(status=completed) completes the first in_progress item."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    _apply(strategy, sid, msg,
           _task_create("u1", "Task A", tool_id="tc1"), source_is_provider_stream=True)
    _apply(strategy, sid, msg,
           _task_update("u2", "1", status="in_progress", tool_id="tu1"), source_is_provider_stream=True)
    _apply(strategy, sid, msg,
           _task_update("u3", "1", status="completed", tool_id="tu2"), source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_tasks") or []
    if len(got) != 1:
        print(f"  expected 1 item, got {len(got)}: {got}")
        return False
    if got[0]["status"] != "completed":
        print(f"  wrong status: {got[0]['status']}")
        return False
    return True


def test_task_update_by_subject_match() -> bool:
    """TaskUpdate with subject matches by content."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    _apply(strategy, sid, msg,
           _task_create("u1", "Fix auth", tool_id="tc1"), source_is_provider_stream=True)
    _apply(strategy, sid, msg,
           _task_create("u2", "Fix tests", tool_id="tc2"), source_is_provider_stream=True)
    _apply(strategy, sid, msg,
           _task_update("u3", "2", status="in_progress",
                        subject="Fix tests", tool_id="tu1"), source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_tasks") or []
    if got[1]["status"] != "in_progress":
        print(f"  second item not in_progress: {got}")
        return False
    if got[0]["status"] != "pending":
        print(f"  first item not pending: {got}")
        return False
    return True


def test_task_update_deleted_removes_item() -> bool:
    """TaskUpdate(status=deleted) with subject removes the matched item."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    _apply(strategy, sid, msg,
           _task_create("u1", "Keep", tool_id="tc1"), source_is_provider_stream=True)
    _apply(strategy, sid, msg,
           _task_create("u2", "Remove me", tool_id="tc2"), source_is_provider_stream=True)
    _apply(strategy, sid, msg,
           _task_update("u3", "2", status="deleted",
                        subject="Remove me", tool_id="tu1"), source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_tasks") or []
    if len(got) != 1:
        print(f"  expected 1 item after delete, got {len(got)}: {got}")
        return False
    if got[0]["content"] != "Keep":
        print(f"  wrong item remained: {got[0]['content']}")
        return False
    return True


def test_task_create_and_todowrite_stored_separately() -> bool:
    """TaskCreate and TodoWrite store into separate fields."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    _apply(strategy, sid, msg,
           _task_create("u1", "From TaskCreate", tool_id="tc1"), source_is_provider_stream=True)
    _apply(strategy, sid, msg,
           _claude_todowrite_native("u2", [
               {"content": "From TodoWrite", "status": "in_progress", "activeForm": "tw"},
           ], tool_id="tw1"), source_is_provider_stream=True)
    todos = session_manager.get(sid).get("current_todos") or []
    tasks = session_manager.get(sid).get("current_tasks") or []
    if len(todos) != 1 or todos[0]["content"] != "From TodoWrite":
        print(f"  wrong todos: {todos}")
        return False
    if len(tasks) != 1 or tasks[0]["content"] != "From TaskCreate":
        print(f"  wrong tasks: {tasks}")
        return False
    return True


def test_task_update_no_match_returns_none() -> bool:
    """TaskUpdate with no matching item returns None."""
    normalized = _task_update("u1", "nonexistent", status="completed", tool_id="tu1")
    result = extract_tasks_from_normalized(normalized, [])
    if result is not None:
        print(f"  expected None, got {result}")
        return False
    return True


def test_task_create_purity() -> bool:
    """TaskCreate MUST NOT mutate `current`."""
    current = [{"content": "existing", "status": "pending", "activeForm": "e"}]
    snapshot = copy.deepcopy(current)
    ev = _task_create("u1", "New task", tool_id="tc1")
    extract_tasks_from_normalized(ev, current)
    if current != snapshot:
        print(f"  TaskCreate mutated current: {current} != {snapshot}")
        return False
    return True


def test_task_update_purity() -> bool:
    """TaskUpdate MUST NOT mutate `current`."""
    current = [{"content": "existing", "status": "pending", "activeForm": "e"}]
    snapshot = copy.deepcopy(current)
    ev = _task_update("u1", "1", status="in_progress", tool_id="tu1")
    extract_tasks_from_normalized(ev, current)
    if current != snapshot:
        print(f"  TaskUpdate mutated current: {current} != {snapshot}")
        return False
    return True


def test_task_convergence_live_equals_recovery() -> bool:
    """Same TaskCreate+TaskUpdate sequence via live and recovery produces
    identical current_tasks."""
    seq = [
        _task_create("u1", "Task A", tool_id="tc1"),
        _task_create("u2", "Task B", tool_id="tc2"),
        _task_update("u3", "1", status="in_progress", tool_id="tu1"),
        _task_update("u4", "1", status="completed", tool_id="tu2"),
        _task_update("u5", "2", status="in_progress", tool_id="tu3"),
    ]

    def run(source_is_provider_stream: bool) -> list:
        sid, msg = _mk_session("native")
        strategy = get_strategy("native")
        for ev in seq:
            _apply(
                strategy, sid, msg, ev,
                source_is_provider_stream=source_is_provider_stream,
            )
        return session_manager.get(sid).get("current_tasks") or []

    live_result = run(True)
    recovery_result = run(False)
    if live_result != recovery_result:
        print(f"  divergence: source_is_provider_stream={live_result} recovery={recovery_result}")
        return False
    return True


def test_fork_derives_tasks_from_copied_messages() -> bool:
    """Fork re-derives current_tasks from TaskCreate events."""
    messages = [{
        "id": "m1", "role": "assistant", "seq": 0,
        "events": [
            _task_create("u1", "Task A", tool_id="tc1"),
            _task_create("u2", "Task B", tool_id="tc2"),
            _task_update("u3", "1", status="in_progress", tool_id="tu1"),
        ],
    }]
    derived = derive_current_tasks(messages)
    if len(derived) != 2:
        print(f"  expected 2 items, got {len(derived)}: {derived}")
        return False
    statuses = [t["status"] for t in derived]
    if statuses != ["in_progress", "pending"]:
        print(f"  wrong statuses: {statuses}")
        return False
    return True


# ── tool_result → taskId tracking tests ─────────────────────────

def test_task_result_stamps_task_id() -> bool:
    """A tool_result for TaskCreate extracts taskId and stamps it on
    the matching item."""
    normalized = _task_create("u1", "My task", tool_id="tc_abc")
    result = extract_tasks_from_normalized(normalized, [])
    if result is None or len(result) != 1:
        print(f"  TaskCreate failed: {result}")
        return False
    if result[0].get("tool_use_id") != "tc_abc":
        print(f"  missing tool_use_id: {result[0]}")
        return False

    # Now feed the tool_result
    tr = _task_create_result("u2", "tc_abc", "42")
    updated = extract_tasks_from_normalized(tr, result)
    if updated is None:
        print("  tool_result returned None")
        return False
    if updated[0].get("task_id") != "42":
        print(f"  task_id not stamped: {updated[0]}")
        return False
    return True


def test_task_update_matches_by_task_id() -> bool:
    """After tool_result stamps task_id, TaskUpdate matches exactly."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    # Create two tasks
    _apply(strategy, sid, msg,
           _task_create("u1", "Task A", tool_id="tc1"), source_is_provider_stream=True)
    _apply(strategy, sid, msg,
           _task_create("u2", "Task B", tool_id="tc2"), source_is_provider_stream=True)
    # tool_results assign taskIds
    _apply(strategy, sid, msg,
           _task_create_result("ur1", "tc1", "1"), source_is_provider_stream=True)
    _apply(strategy, sid, msg,
           _task_create_result("ur2", "tc2", "2"), source_is_provider_stream=True)
    # Update Task B (id=2) to in_progress — NOT Task A
    _apply(strategy, sid, msg,
           _task_update("u3", "2", status="in_progress", tool_id="tu1"), source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_tasks") or []
    if got[0]["status"] != "pending":
        print(f"  Task A should be pending: {got}")
        return False
    if got[1]["status"] != "in_progress":
        print(f"  Task B should be in_progress: {got}")
        return False
    return True


def test_task_update_matches_out_of_order() -> bool:
    """TaskUpdate matches by taskId even when working out of order."""
    sid, msg = _mk_session("native")
    strategy = get_strategy("native")
    _apply(strategy, sid, msg,
           _task_create("u1", "Task A", tool_id="tc1"), source_is_provider_stream=True)
    _apply(strategy, sid, msg,
           _task_create("u2", "Task B", tool_id="tc2"), source_is_provider_stream=True)
    _apply(strategy, sid, msg,
           _task_create("u3", "Task C", tool_id="tc3"), source_is_provider_stream=True)
    _apply(strategy, sid, msg,
           _task_create_result("ur1", "tc1", "1"), source_is_provider_stream=True)
    _apply(strategy, sid, msg,
           _task_create_result("ur2", "tc2", "2"), source_is_provider_stream=True)
    _apply(strategy, sid, msg,
           _task_create_result("ur3", "tc3", "3"), source_is_provider_stream=True)
    # Complete Task C (id=3) — skip A and B
    _apply(strategy, sid, msg,
           _task_update("u4", "3", status="completed", tool_id="tu1"), source_is_provider_stream=True)
    # Start Task A (id=1)
    _apply(strategy, sid, msg,
           _task_update("u5", "1", status="in_progress", tool_id="tu2"), source_is_provider_stream=True)
    got = session_manager.get(sid).get("current_tasks") or []
    statuses = [t["status"] for t in got]
    if statuses != ["in_progress", "pending", "completed"]:
        print(f"  wrong statuses (should be exact match): {statuses}")
        return False
    return True


def test_task_result_ignores_already_stamped() -> bool:
    """Re-playing a tool_result on an item that already has a task_id
    is a no-op (idempotent replay)."""
    normalized = _task_create("u1", "My task", tool_id="tc_abc")
    result = extract_tasks_from_normalized(normalized, [])
    tr = _task_create_result("u2", "tc_abc", "42")
    updated = extract_tasks_from_normalized(tr, result)
    # Replay the same result
    updated2 = extract_tasks_from_normalized(tr, updated)
    if updated2 is not None:
        print(f"  replay should be no-op, got {updated2}")
        return False
    return True


def test_task_result_no_match_is_noop() -> bool:
    """A tool_result that doesn't match any pending item is a no-op."""
    current = [{"content": "existing", "status": "pending", "source_id": "tc:abc"}]
    tr = _task_create_result("u1", "nonexistent_id", "99")
    result = extract_tasks_from_normalized(tr, current)
    if result is not None:
        print(f"  expected None for non-matching result, got {result}")
        return False
    return True


def test_disabled_todos_extension_blocks_session_mutation() -> bool:
    import session_event_extensions

    todos_id = extension_store.BUILTIN_TODOS_EXTENSION_ID
    original_enqueue = session_event_extensions._enqueue_external_hook
    jobs = []
    session_event_extensions._enqueue_external_hook = jobs.append
    extension_store.set_enabled(todos_id, False)
    try:
        sid, msg = _mk_session("native")
        strategy = get_strategy("native")
        _apply(
            strategy,
            sid,
            msg,
            _claude_todowrite_native(
                "disabled-todos",
                [{"content": "Should not land", "status": "pending"}],
            ),
            source_is_provider_stream=True,
        )
        if jobs:
            return False
        _apply(
            strategy,
            sid,
            msg,
            _task_create("disabled-task", "Should not land"),
            source_is_provider_stream=True,
        )
        return not jobs
    finally:
        extension_store.set_enabled(todos_id, True)
        session_event_extensions._enqueue_external_hook = original_enqueue


def test_builtin_todos_projection_does_not_dispatch_extension_backend() -> bool:
    import extension_backend_loader
    import session_event_extensions

    original_hooks = extension_store.session_event_hooks
    original_invoke = extension_backend_loader.invoke_extension_backend_sync
    original_enqueue = session_event_extensions._enqueue_external_hook
    calls: list[str] = []
    jobs = []

    def fake_invoke(extension_id, path, **kwargs):
        calls.append(extension_id)
        return 500, b"{}"

    extension_store.session_event_hooks = lambda: [
        (extension_store.BUILTIN_TODOS_EXTENSION_ID, "/session-event"),
    ]
    extension_backend_loader.invoke_extension_backend_sync = fake_invoke
    session_event_extensions._enqueue_external_hook = jobs.append
    try:
        sid, msg = _mk_session("native")
        strategy = get_strategy("native")
        _apply(
            strategy,
            sid,
            msg,
            _claude_todowrite_native(
                "builtin-local",
                [{"content": "In process", "status": "pending"}],
            ),
            source_is_provider_stream=True,
        )
        return (
            len(jobs) == 1
            and jobs[0].spec.extension_id == extension_store.BUILTIN_TODOS_EXTENSION_ID
            and calls == []
        )
    finally:
        extension_store.session_event_hooks = original_hooks
        extension_backend_loader.invoke_extension_backend_sync = original_invoke
        session_event_extensions._enqueue_external_hook = original_enqueue


def test_builtin_todos_worker_applies_projection() -> bool:
    import session_event_extensions

    sid, _msg = _mk_session("native")
    job = session_event_extensions.ExtensionHookJob(
        spec=session_event_extensions.SessionEventHookSpec(
            extension_id=extension_store.BUILTIN_TODOS_EXTENSION_ID,
            path="",
            readable_fields=frozenset({"current_todos", "current_tasks"}),
            mutable_fields=frozenset({"current_todos", "current_tasks"}),
        ),
        session_id=sid,
        normalized=_claude_todowrite_native(
            "builtin-worker",
            [{"content": "Worker projection", "status": "pending"}],
        ),
        session_fields={},
        use_sdk=True,
    )
    session_event_extensions._run_builtin_todos_job(job)
    got = session_manager.get(sid).get("current_todos") or []
    return (
        len(got) == 1
        and got[0].get("content") == "Worker projection"
        and got[0].get("status") == "pending"
    )


def test_session_event_bridge_dispatches_non_todos_hooks() -> bool:
    import extension_backend_loader
    import session_event_extensions

    original_hooks = extension_store.session_event_hooks
    original_allowlist = extension_store.session_field_allowlist
    original_read_allowlist = extension_store.session_field_read_allowlist
    original_invoke = extension_backend_loader.invoke_extension_backend_sync
    calls: list[tuple[str, str]] = []
    bodies: list[dict] = []

    def fake_hooks() -> list[tuple[str, str]]:
        return [("ofek-dev.other", "/session-event")]

    def fake_allowlist(extension_id: str) -> list[str]:
        return ["current_tasks"] if extension_id == "ofek-dev.other" else []

    def fake_read_allowlist(extension_id: str) -> list[str]:
        return ["current_tasks"] if extension_id == "ofek-dev.other" else []

    def fake_invoke(extension_id, path, **kwargs):
        calls.append((extension_id, path))
        bodies.append(json.loads(kwargs["body_bytes"].decode("utf-8")))
        return 200, json.dumps({
            "session_fields": {
                "current_tasks": [{"content": "Other hook ran", "status": "pending"}],
            },
        }).encode("utf-8")

    extension_store.session_field_allowlist = fake_allowlist
    extension_store.session_field_read_allowlist = fake_read_allowlist
    extension_backend_loader.invoke_extension_backend_sync = fake_invoke
    try:
        sid, _msg = _mk_session("native")
        job = session_event_extensions.ExtensionHookJob(
            spec=session_event_extensions.SessionEventHookSpec(
                extension_id="ofek-dev.other",
                path="/session-event",
                readable_fields=frozenset({"current_tasks"}),
                mutable_fields=frozenset({"current_tasks"}),
            ),
            session_id=sid,
            normalized={"type": "agent_message", "data": {}},
            session_fields={"current_tasks": []},
            use_sdk=True,
        )
        session_event_extensions._run_extension_hook_job(job)
        fields = session_manager.get(sid).get("current_tasks") or []
    finally:
        extension_store.session_event_hooks = original_hooks
        extension_store.session_field_allowlist = original_allowlist
        extension_store.session_field_read_allowlist = original_read_allowlist
        extension_backend_loader.invoke_extension_backend_sync = original_invoke
    return (
        calls == [("ofek-dev.other", "session-event")]
        and bodies[0].get("session_fields") == {"current_tasks": []}
        and bool(fields)
    )


def test_session_event_bridge_filters_undeclared_hook_fields() -> bool:
    import extension_backend_loader
    import session_event_extensions

    original_hooks = extension_store.session_event_hooks
    original_allowlist = extension_store.session_field_allowlist
    original_read_allowlist = extension_store.session_field_read_allowlist
    original_invoke = extension_backend_loader.invoke_extension_backend_sync
    bodies: list[dict] = []

    extension_store.session_field_allowlist = lambda extension_id: []
    extension_store.session_field_read_allowlist = lambda extension_id: []

    def fake_invoke(*args, **kwargs):
        bodies.append(json.loads(kwargs["body_bytes"].decode("utf-8")))
        return (
            200,
            json.dumps({
                "session_fields": {
                    "current_tasks": [{"content": "No permission", "status": "pending"}],
                    "supervisor_enabled": True,
                },
            }).encode("utf-8"),
        )

    extension_backend_loader.invoke_extension_backend_sync = fake_invoke
    try:
        sid, _msg = _mk_session("native")
        job = session_event_extensions.ExtensionHookJob(
            spec=session_event_extensions.SessionEventHookSpec(
                extension_id="ofek-dev.other",
                path="/session-event",
                readable_fields=frozenset(),
                mutable_fields=frozenset(),
            ),
            session_id=sid,
            normalized={"type": "agent_message", "data": {}},
            session_fields={},
            use_sdk=True,
        )
        session_event_extensions._run_extension_hook_job(job)
        fields = session_manager.get(sid).get("current_tasks") or []
    finally:
        extension_store.session_event_hooks = original_hooks
        extension_store.session_field_allowlist = original_allowlist
        extension_store.session_field_read_allowlist = original_read_allowlist
        extension_backend_loader.invoke_extension_backend_sync = original_invoke
    return fields == [] and bodies[0].get("session_fields") == {}


def test_session_event_hook_discovery_failure_is_nonfatal() -> bool:
    import session_event_extensions

    original_hooks = extension_store.session_event_hooks
    todos_id = extension_store.BUILTIN_TODOS_EXTENSION_ID
    extension_store.session_event_hooks = lambda: (_ for _ in ()).throw(
        extension_store.ExtensionError("stale extension manifest")
    )
    extension_store.set_enabled(todos_id, False)
    try:
        return session_event_extensions.apply_event(
            "sid",
            {"type": "agent_message", "data": {}},
            use_sdk=True,
        ) is False
    finally:
        extension_store.set_enabled(todos_id, True)
        extension_store.session_event_hooks = original_hooks


def test_session_event_hook_dispatch_failure_is_nonfatal() -> bool:
    import extension_backend_loader
    import session_event_extensions

    original_hooks = extension_store.session_event_hooks
    original_allowlist = extension_store.session_field_allowlist
    original_read_allowlist = extension_store.session_field_read_allowlist
    original_invoke = extension_backend_loader.invoke_extension_backend_sync

    extension_store.session_field_allowlist = lambda extension_id: ["current_tasks"]
    extension_store.session_field_read_allowlist = lambda extension_id: ["current_tasks"]

    def failing_invoke(*args, **kwargs):
        raise RuntimeError("extension backend crashed")

    extension_backend_loader.invoke_extension_backend_sync = failing_invoke
    try:
        sid, _msg = _mk_session("native")
        job = session_event_extensions.ExtensionHookJob(
            spec=session_event_extensions.SessionEventHookSpec(
                extension_id="ofek-dev.other",
                path="/session-event",
                readable_fields=frozenset({"current_tasks"}),
                mutable_fields=frozenset({"current_tasks"}),
            ),
            session_id=sid,
            normalized={"type": "agent_message", "data": {}},
            session_fields={"current_tasks": []},
            use_sdk=True,
        )
        session_event_extensions._run_extension_hook_job(job)
        fields = session_manager.get(sid).get("current_tasks") or []
    finally:
        extension_store.session_event_hooks = original_hooks
        extension_store.session_field_allowlist = original_allowlist
        extension_store.session_field_read_allowlist = original_read_allowlist
        extension_backend_loader.invoke_extension_backend_sync = original_invoke
    return fields == []


def test_apply_event_enqueues_non_todos_hooks_without_inline_dispatch() -> bool:
    import session_event_extensions

    original_hooks = extension_store.session_event_hooks
    original_allowlist = extension_store.session_field_allowlist
    original_read_allowlist = extension_store.session_field_read_allowlist
    original_enqueue = session_event_extensions._enqueue_external_hook
    todos_id = extension_store.BUILTIN_TODOS_EXTENSION_ID
    jobs = []

    extension_store.session_event_hooks = lambda: [("ofek-dev.other", "/session-event")]
    extension_store.session_field_allowlist = lambda extension_id: ["current_tasks"]
    extension_store.session_field_read_allowlist = lambda extension_id: ["current_tasks"]
    session_event_extensions._enqueue_external_hook = jobs.append
    extension_store.set_enabled(todos_id, False)
    try:
        changed = session_event_extensions.apply_event(
            "sid",
            {"type": "agent_message", "data": {}},
            use_sdk=True,
        )
    finally:
        extension_store.session_event_hooks = original_hooks
        extension_store.session_field_allowlist = original_allowlist
        extension_store.session_field_read_allowlist = original_read_allowlist
        session_event_extensions._enqueue_external_hook = original_enqueue
        extension_store.set_enabled(todos_id, True)
    return (
        changed is True
        and len(jobs) == 1
        and jobs[0].spec.extension_id == "ofek-dev.other"
        and jobs[0].session_id == "sid"
    )


TESTS = [
    ("Claude TodoWrite first call sets list", test_claude_todowrite_first_call_sets_list),
    ("Claude two sequential → union merge", test_claude_two_sequential_todowrites_union_merge),
    ("Claude TodoWrite UNION keeps completed across phases", test_claude_todowrite_union_keeps_completed_across_phases),
    ("Gemini single update_topic → in_progress", test_gemini_single_update_topic_appends_in_progress),
    ("Gemini three sequential → prior completed", test_gemini_three_sequential_prior_completed),
    ("Gemini same source_id same content → no-op", test_gemini_same_source_id_same_content_noop),
    ("Gemini same source_id mutated → replace", test_gemini_same_source_id_mutated_content_replaces),
    ("Gemini tool_id pattern mismatch → content-hash", test_gemini_tool_id_pattern_mismatch_uses_content_hash),
    ("Extractor skip when tool_id missing", test_extractor_skip_when_tool_id_missing),
    ("Extractor skip non-serializable Gemini input", test_extractor_skip_non_serializable_gemini_input),
    ("Extractor purity (current unmutated)", test_extractor_purity_does_not_mutate_current),
    ("worker_event TodoWrite does NOT touch session todos", test_worker_event_todowrite_does_not_touch_session_todos),
    ("interleaved manager + agent_message → accumulates", test_interleaved_manager_and_agent_message_accumulates),
    ("convergence invariant: Claude source_is_provider_stream==recovery", test_convergence_invariant_live_equals_recovery),
    ("convergence invariant: Gemini source_is_provider_stream==recovery", test_convergence_invariant_gemini_replay),
    ("equality precheck suppresses redundant fires", test_equality_skip_suppresses_redundant_fires),
    ("Claude in_progress NOT demoted by Gemini delta", test_claude_in_progress_not_demoted_by_gemini_delta),
    ("two Gemini deltas → only Gemini-owned prior completes", test_two_gemini_deltas_prior_completes_only_self),
    ("fork derives current_todos from copied messages", test_fork_derives_current_todos_from_copied_messages),
    ("fork derivation skips worker panel events", test_fork_skip_worker_panel_events),
    ("concurrent Gemini deltas: no lost update (TOCTOU)", test_concurrent_gemini_deltas_no_lost_update),
    ("hydration loads current_todos from events.jsonl", test_hydration_loads_current_todos_from_events_jsonl),
    ("Gemini re-emission preserves completed", test_gemini_reemission_preserves_completed_status),
    ("_load_root derives current_todos including orphans", test_load_derives_current_todos_from_orphan_rows),
    ("cli_prompt reminder: open todos only", test_cli_prompt_open_todos_only),
    ("ALL_TASKS__DONE marker completes todos and suppresses reminder", test_all_tasks_done_marker_completes_todos_and_suppresses_reminder),
    ("dispatch supervisor branch passes user_initiated=True", test_dispatch_supervisor_branch_passes_user_initiated),
    ("run_turn actually calls _append_todo_reminder (AST)", test_run_turn_actually_calls_append_todo_reminder),
    ("run_turn gate covers every non-empty user prompt",
        test_run_turn_gate_covers_every_user_prompt),
    ("get_current_todos_snapshot returns copy", test_get_current_todos_snapshot_returns_copy),
    ("Codex todo_list: first incomplete → in_progress", test_codex_todo_list_first_incomplete_is_in_progress),
    ("Codex todo_list: completed then first incomplete", test_codex_todo_list_completed_then_first_incomplete),
    ("Codex todo_list: all completed → no in_progress", test_codex_todo_list_all_completed_no_in_progress),
    ("Codex todo_list: stable uuid replaces in place", test_codex_todo_list_stable_uuid_replaces_in_place),
    ("Codex distinct item ids → distinct render nodes", test_codex_distinct_items_each_render),
    ("Codex todo_list convergence: source_is_provider_stream==recovery", test_codex_todo_list_convergence_live_equals_recovery),
    ("Codex update_plan: maps plan steps to current_todos", test_codex_update_plan_maps_to_todos),
    ("Codex update_plan: status progression union-merges", test_codex_update_plan_status_progression_union_merge),
    ("Codex update_plan convergence: source_is_provider_stream==recovery", test_codex_update_plan_convergence_live_equals_recovery),
    ("TaskCreate adds pending item", test_task_create_adds_pending_item),
    ("TaskCreate multiple accumulates", test_task_create_multiple_accumulates),
    ("TaskCreate dedup on replay", test_task_create_dedup_on_replay),
    ("TaskUpdate heuristic: pending→in_progress", test_task_update_status_heuristic_pending_to_in_progress),
    ("TaskUpdate heuristic: in_progress→completed", test_task_update_status_heuristic_to_completed),
    ("TaskUpdate by subject match", test_task_update_by_subject_match),
    ("TaskUpdate deleted removes item", test_task_update_deleted_removes_item),
    ("TaskCreate then TodoWrite stored separately", test_task_create_and_todowrite_stored_separately),
    ("TaskUpdate no match returns None", test_task_update_no_match_returns_none),
    ("TaskCreate purity (current unmutated)", test_task_create_purity),
    ("TaskUpdate purity (current unmutated)", test_task_update_purity),
    ("Task convergence: source_is_provider_stream==recovery", test_task_convergence_live_equals_recovery),
    ("Fork derives tasks from copied messages", test_fork_derives_tasks_from_copied_messages),
    ("tool_result stamps task_id", test_task_result_stamps_task_id),
    ("TaskUpdate matches by task_id (exact)", test_task_update_matches_by_task_id),
    ("TaskUpdate matches out-of-order by task_id", test_task_update_matches_out_of_order),
    ("tool_result replay is idempotent", test_task_result_ignores_already_stamped),
    ("tool_result no-match is no-op", test_task_result_no_match_is_noop),
    ("Disabled Todos extension blocks session mutation", test_disabled_todos_extension_blocks_session_mutation),
    ("Builtin Todos projection does not dispatch extension backend", test_builtin_todos_projection_does_not_dispatch_extension_backend),
    ("Builtin Todos worker applies projection", test_builtin_todos_worker_applies_projection),
    ("Session-event bridge dispatches non-Todos hooks", test_session_event_bridge_dispatches_non_todos_hooks),
    ("Session-event bridge filters undeclared hook fields", test_session_event_bridge_filters_undeclared_hook_fields),
    ("Session-event hook discovery failure is nonfatal", test_session_event_hook_discovery_failure_is_nonfatal),
    ("Session-event hook dispatch failure is nonfatal", test_session_event_hook_dispatch_failure_is_nonfatal),
    ("apply_event enqueues non-Todos hooks without inline dispatch", test_apply_event_enqueues_non_todos_hooks_without_inline_dispatch),
]


def main_run() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                ok = fn()
            except Exception as e:
                ok = False
                import traceback
                traceback.print_exc()
                print(f"  exception: {e}")
            print(f"{PASS if ok else FAIL}  {name}")
            if not ok:
                failed += 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print()
    if failed:
        print(f"{failed} of {len(TESTS)} test(s) FAILED")
    else:
        print(f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
