"""Baseline regression test for native-mode session jsonl ingestion.

Pins the three-scenario convergence invariant from CLAUDE.md against
REAL native-mode claude session jsonl fixtures (not synthetic events):

  C1 ``replay_via_apply_event`` — line-by-line ``enrich_jsonl_line`` +
     ``strategy.apply_event(source_is_provider_stream=True)``, then end-of-turn finalization
     (``set_streaming(False)`` → ``update_running_content``).
     NOTE: this is NOT the live SDK callback path — it bypasses the
     orchestrator's framing (``save_ws_callback`` /
     ``_apply_event_to_assistant_msg``). It locks the ``apply_event``
     funnel semantics on real-data shape; SDK orchestration sits above
     and is out of scope.

  C2 ``recovery_replay`` — seeds an orphan run_dir whose ``state.json``
     points at the fixture, calls ``run_recovery._replay_and_apply``
     directly. (Independent: a fresh session, fresh events.jsonl.)

  C3 ``frontend_offline_reconcile`` — starts from C1's final state,
     clears ``msg.events`` in place, calls
     ``main._reconcile_msg_events_from_jsonl`` over the same root tree.
     Reads the same events.jsonl C1 wrote — this models the
     "backend online, frontend offline → reconnect" scenario where the
     persisted render tree lags events.jsonl. An independent-writer
     reconcile (e.g. ``source="claude_tailer"``) is already locked by
     ``test_recovery_render_consistency.py:168-219`` and intentionally
     NOT re-asserted here.

Invariants asserted per fixture:
  - C1 render-fingerprint == C2 render-fingerprint == C3 render-fingerprint
  - C1 jsonl-fingerprint == C2 jsonl-fingerprint
  - All match the checked-in baseline JSON (``<fixture>.baseline.json``)

Out of scope (intentional, NOT a bug if these diverge here):
  - SDK callback path / orchestrator framing
  - Worker-fork tailer (different ``source``, different writer)
  - manager / supervisor modes
  - Gemini provider
  - Synthetic events (``"<synthetic>"`` marker)
  - (Subagent fan-out IS covered for the ``with_subagent.jsonl`` fixture;
    the test also asserts a hand-derived invariant table for it — see
    ``_assert_hand_derived_invariants`` for `with_subagent.jsonl`,
    `multi_subagent.jsonl`, and `heavy_thinking.jsonl`.)
  - Positive bcfile rewrite coverage (fixtures use redacted ``/Users/dev``
    paths AND seeded session uses ``cwd="/tmp"`` so rewriter never fires;
    ``bcfile_substring_in_data`` is locked at False as a defensive guard)

Usage:
  python scripts/test_native_jsonl_baseline.py              # CI: assert vs baseline
  python scripts/test_native_jsonl_baseline.py --regenerate # dry: print report only
  python scripts/test_native_jsonl_baseline.py --regenerate --accept  # write baselines
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

# INVARIANT: BETTER_CLAUDE_HOME tempdir MUST be set BEFORE any backend
# import — backend modules read ba_home() at import time.
import _test_home
_TMP_HOME = _test_home.isolate("bc-test-native-baseline-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Import `main` once at module top so its top-level side-effects
# (FastAPI app construction, broadcaster wiring, listener registration)
# fire BEFORE any caller runs — keeps listener state uniform across
# C1/C2/C3 instead of having C3's lazy import attach mid-test.
import main as _main  # noqa: E402, F401

from session_manager import manager as session_manager  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from claude_jsonl_enrich import enrich_jsonl_line  # noqa: E402
from provider_claude import _SubagentRegistry, _runs_root  # noqa: E402
from run_recovery import _replay_and_apply, _replay_subagents  # noqa: E402
from event_shape import extract_output_text  # noqa: E402

# Pin the session_manager's bound event loop to None. Default is None
# anyway (session_manager.py:135), but pinning makes the test resilient
# to harnesses that bind a loop and would otherwise schedule listener
# side-effects (bus publishes, trace writes) that pollute fingerprints
# across C1/C2/C3.
session_manager._loop = None

# Disable file_ref_resolver bcfile rewrites for the duration of the test.
# Reason: the rewriter MUTATES event data in-place inside apply_event
# (orchs/base.py:581) when an absolute path in the fixture happens to
# exist on the developer machine. That mutation creates a real C1↔C2
# divergence — C2's _replay_and_apply (run_recovery.py:603) computes
# msg.content from PRE-mutation events while C1 computes from POST.
# The rewriter has its own dedicated coverage; this test locks the
# apply_event funnel's render-tree behavior, not the rewriter's path
# resolution. Patching `_cache.exists` makes every `bcfile:` decision
# resolve to "file doesn't exist" → no rewrite fires.
from file_ref_resolver import _cache as _ffr_cache  # noqa: E402
_ffr_cache.exists = lambda _p: False  # type: ignore[assignment]

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
WARN = "\x1b[33mWARN\x1b[0m"

FIXTURES_DIR = Path(_HERE) / "fixtures" / "native_sessions"
FIXTURE_NAMES = [
    "text_only.jsonl",
    "tool_use_bash.jsonl",
    "tool_use_bash_read.jsonl",
    "with_subagent.jsonl",       # 1 subagent
    "multi_subagent.jsonl",      # 4 subagents → FIFO claim ordering + multi parent_tool_use_id
    "heavy_thinking.jsonl",      # 6 thinking blocks across 18 assistant lines, no subagent
]

# Markers a native fixture must NOT contain (would route through paths
# this test doesn't cover, OR carry leaked PII).
FORBIDDEN_MARKERS = (
    "<synthetic>",
    "/workspace",
    "ghp_",
    "sk-ant-",
    "naturalseo",
)
# Tools never allowed in any fixture (manager-mode delegation paths).
ALWAYS_FORBIDDEN_TOOLS = ("delegate",)


def _validate_fixture(path: Path) -> None:
    text = path.read_text()
    for marker in FORBIDDEN_MARKERS:
        if marker in text:
            raise SystemExit(
                f"fixture {path.name} contains forbidden marker {marker!r}"
            )
    for tool in ALWAYS_FORBIDDEN_TOOLS:
        if f'"name":"{tool}"' in text:
            raise SystemExit(
                f"fixture {path.name} contains forbidden tool {tool!r}"
            )
    if '"name":"mcp__' in text:
        raise SystemExit(
            f"fixture {path.name} contains forbidden mcp__-prefixed tool"
        )

    # Subagent invariant: fixture has Agent/Task tool_use <=> sidecar
    # dir exists. Either both or neither — otherwise the fixture is
    # mis-shaped (orphan tool_use without sidecar would silently drop
    # subagent events; sidecar without tool_use is dead-code).
    sidecar = path.parent / path.stem / "subagents"
    has_agent_call = '"name":"Agent"' in text or '"name":"Task"' in text
    if has_agent_call and not sidecar.exists():
        raise SystemExit(
            f"fixture {path.name} has Agent/Task tool_use but no sidecar at {sidecar}"
        )
    if sidecar.exists() and not has_agent_call:
        raise SystemExit(
            f"fixture {path.name} has sidecar dir but no Agent/Task tool_use"
        )


# ─── helpers ────────────────────────────────────────────────────────


def _sha(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _enrich_all(fixture_path: Path) -> list[dict]:
    """Enrich every raw line — mirrors ``run_recovery._replay_from_claude_jsonl``
    + ``_replay_subagents``: parent first (in line order), then each
    sidecar subagent jsonl with ``parent_tool_use_id`` injected.

    Does NOT slice by ``pre_query_byte_offset`` (the fixture IS the full
    turn from line 0).
    """
    registry = _SubagentRegistry()
    u2tids: dict[str, list[str]] = {}
    u2par: dict[str, str] = {}
    out: list[dict] = []
    # Parent — also populates registry with Agent/Task tool_use_ids
    # (via register_agent_tool_uses inside enrich_jsonl_line).
    for raw in fixture_path.read_text().splitlines():
        ev = enrich_jsonl_line(raw, u2tids, u2par, registry)
        if ev is not None:
            out.append(ev)
    # Subagent fan-out — delegate to the shared helper so the test
    # exercises the SAME code path C2's recovery uses.
    out.extend(_replay_subagents(fixture_path, registry))
    return out


def _fresh_native_session(cwd: str = "/tmp") -> tuple[str, dict, dict]:
    """Returns (app_sid, user_msg, asst_msg).

    ``cwd="/tmp"`` is intentional — fixtures use ``/Users/dev`` paths
    that don't overlap, so file_ref_resolver never rewrites. Keeps the
    data dict deterministic between C1/C2 (source_is_provider_stream=True) and C3 (source_is_provider_stream=False).
    """
    sess = session_manager.create(
        name="baseline-test",
        model="claude-sonnet",
        cwd=cwd,
        orchestration_mode="native",
    )
    sid = sess["id"]
    strategy = get_strategy("native")
    user_msg = {
        "id": str(uuid.uuid4()),
        "role": "user",
        "content": "do work",
        "events": [],
        "isStreaming": False,
    }
    asst_msg = strategy.build_assistant_scaffold()
    session_manager.append_user_msg(sid, user_msg)
    session_manager.append_assistant_msg(sid, asst_msg)
    return sid, user_msg, asst_msg


def _events_jsonl_path(root_id: str) -> Path:
    return Path(_TMP_HOME) / "sessions" / root_id / "events.jsonl"


def _read_events_jsonl(root_id: str) -> list[dict]:
    path = _events_jsonl_path(root_id)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


# ─── fingerprint ────────────────────────────────────────────────────


_BCFILE_MARKER = "bcfile:"


def _has_bcfile(obj) -> bool:
    """Recursive substring scan for ``bcfile:`` in any string value."""
    if isinstance(obj, str):
        return _BCFILE_MARKER in obj
    if isinstance(obj, dict):
        return any(_has_bcfile(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_bcfile(x) for x in obj)
    return False


def _classify_event_type(ev: dict) -> str:
    """Returns a stable type tag for ``events_by_type`` counts.

    For claude agent_message events, returns ``agent_message:<inner_type>``
    where ``<inner_type>`` is the top-level claude line type (e.g.
    ``assistant``, ``user``, ``attachment``, ``system``). Other event
    wrappers return their outer type.
    """
    outer = ev.get("type") or "?"
    data = ev.get("data") or {}
    inner = data.get("type") if isinstance(data, dict) else None
    if outer == "agent_message" and inner:
        return f"agent_message:{inner}"
    return outer


def _render_fingerprint(msg: dict) -> dict:
    """Fingerprint of the render-tree state for a single assistant msg.

    Asserted equal across C1==C2==C3. Strips non-deterministic fields
    (timestamps, run_ids, msg_ids) — only data identity matters.
    """
    events: list[dict] = list(msg.get("events") or [])
    uuids_in_order: list[str] = []
    data_shas: list[str] = []
    tuple_shas: list[str] = []
    by_type: dict[str, int] = {}
    for ev in events:
        data = ev.get("data") or {}
        # _event_uuid mirrors the resolver in orchs/base.py:71-91
        uid = ""
        if isinstance(data, dict):
            uid = data.get("uuid") or ""
            if not uid:
                inner = data.get("event")
                if isinstance(inner, dict):
                    inner_data = inner.get("data")
                    if isinstance(inner_data, dict):
                        uid = inner_data.get("uuid") or ""
        uuids_in_order.append(uid)
        data_shas.append(_sha(data))
        # tuple shape catches subagent / tool-routing wire-up regressions
        tool_name = ""
        tool_use_id = ""
        parent_tool_use_id = data.get("parent_tool_use_id") or ""
        message = data.get("message") if isinstance(data, dict) else None
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "tool_use":
                        tool_name = blk.get("name") or ""
                        tool_use_id = blk.get("id") or ""
                        break
        tuple_shas.append(_sha((uid, parent_tool_use_id, tool_name, tool_use_id)))
        tag = _classify_event_type(ev)
        by_type[tag] = by_type.get(tag, 0) + 1

    content_from_events = extract_output_text(events) if events else ""
    return {
        "events_count": len(events),
        "events_by_type": by_type,
        "events_uuid_list_sha256": _sha(uuids_in_order),
        "events_data_sha256_list": _sha(data_shas),
        "events_tuple_sha256": _sha(tuple_shas),
        "content_sha256": _sha(content_from_events),
        "content_length": len(content_from_events),
        "msg_content_sha256": _sha(msg.get("content") or ""),
        "has_manager_scope": False,
        "bcfile_substring_in_data": _has_bcfile(events),
    }


def _jsonl_fingerprint(root_id: str) -> dict:
    """Fingerprint of events.jsonl for a root. Strips seq/ts/sid/run_id/
    msg_id (vary per run) — locks ordered `data` shape only.
    """
    entries = _read_events_jsonl(root_id)
    data_shas = [_sha(e.get("data") or {}) for e in entries]
    return {
        "events_jsonl_count": len(entries),
        "events_jsonl_data_sha256_list": _sha(data_shas),
    }


# ─── three callers ──────────────────────────────────────────────────


def _run_c1(fixture_path: Path) -> tuple[str, str, list[dict]]:
    """C1: enrich every raw line → apply_event(source_is_provider_stream=True) per event →
    end-of-turn finalize (set_streaming(False) → update_running_content).

    Returns (app_sid, asst_msg_id, enriched_events) so C3 can chain.
    """
    sid, user_msg, asst_msg = _fresh_native_session()
    asst_id = asst_msg["id"]
    root_id = session_manager._root_id_for(sid)
    strategy = get_strategy("native")
    enriched_events = _enrich_all(fixture_path)
    ctx = ApplyEventCtx(
        manager_sid_holder={"id": None},
        workers_list=[],
        user_msg=user_msg,
        root_id=root_id,
        run_id=str(uuid.uuid4()),
    )
    # Wrap apply_event loop in a single batch — matches
    # save_ws_callback (orchestrator.py:1982) and _apply_integration_sync
    # (run_recovery.py:532) production pattern.
    with session_manager.batch(sid):
        sess = session_manager.get(sid)
        last_asst = next(m for m in sess["messages"] if m["id"] == asst_id)
        for ev in enriched_events:
            strategy.apply_event(
                app_session_id=sid,
                msg=last_asst,
                event=ev,
                ctx=ctx,
                source_is_provider_stream=True,
            )
    # End-of-turn finalization. INVARIANT: set_streaming BEFORE
    # update_running_content — matches orchestrator.py:2745→2753 and
    # _apply_completion_state→_replay_and_apply ordering.
    session_manager.set_streaming(sid, asst_id, False)
    extracted = extract_output_text(enriched_events) if enriched_events else ""
    if extracted:
        session_manager.update_running_content(sid, asst_id, extracted)
    return sid, asst_id, enriched_events


def _seed_orphan_run_from_fixture(
    app_sid: str, fixture_path: Path, claude_sid: str,
) -> str:
    """Build a recovery-shaped run_dir with the fixture as the claude
    session jsonl. Mirrors test_recovery_render_consistency.py:85-114
    but COPIES the fixture instead of synthesizing events."""
    run_id = str(uuid.uuid4())
    run_dir = _runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    claude_jsonl = run_dir / "fake_claude" / f"{claude_sid}.jsonl"
    claude_jsonl.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(fixture_path, claude_jsonl)
    # Sidecar dir: if the fixture has a <stem>/subagents/ dir alongside
    # it, copy it next to the run_dir's claude_jsonl so
    # run_recovery._replay_subagents can find it. Path shape mirrors
    # claude CLI's layout: <jsonl_stem>/subagents/agent-*.{jsonl,meta.json}.
    src_sidecar = fixture_path.parent / fixture_path.stem / "subagents"
    if src_sidecar.is_dir():
        dst_sidecar = claude_jsonl.parent / claude_jsonl.stem / "subagents"
        dst_sidecar.mkdir(parents=True, exist_ok=True)
        for f in src_sidecar.iterdir():
            if f.is_file():
                shutil.copy(f, dst_sidecar / f.name)
    (run_dir / "input.json").write_text(json.dumps({
        "prompt": "do work", "cwd": "/tmp", "model": "claude-sonnet",
        "session_id": claude_sid, "mode": "native",
        "app_session_id": app_sid, "fork": False,
    }))
    (run_dir / "state.json").write_text(json.dumps({
        "run_id": run_id, "mode": "native", "runner_pid": 0,
        "app_session_id": app_sid, "session_id": claude_sid,
        "jsonl_path": str(claude_jsonl), "pre_query_byte_offset": 0,
        "complete": False,
    }))
    (run_dir / "backend_state.json").write_text(json.dumps({
        "run_id": run_id, "app_session_id": app_sid, "mode": "native",
        "runner_pid": 0, "session_id": claude_sid,
        "jsonl_path": str(claude_jsonl), "processed_byte": 0,
        "cancelled": False,
    }))
    (run_dir / "pid").write_text("0")
    return run_id


def _run_c2(fixture_path: Path) -> tuple[str, str]:
    """C2: orphan run_dir → ``_replay_and_apply`` directly (the funnel
    CLAUDE.md names) → completion-state finalization."""
    sid, _user, asst_msg = _fresh_native_session()
    asst_id = asst_msg["id"]
    claude_sid = str(uuid.uuid4())
    run_id = _seed_orphan_run_from_fixture(sid, fixture_path, claude_sid)
    with session_manager.batch(sid, bump_updated_at=False):
        sess = session_manager.get(sid)
        last_asst = next(m for m in sess["messages"] if m["id"] == asst_id)
        _replay_and_apply(
            persist_sid=sid,
            run_id=run_id,
            mode="native",
            claude_sid=claude_sid,
            sess=sess,
            last_asst=last_asst,
            msg_id=asst_id,
        )
    # _replay_and_apply writes content via update_running_content; match
    # the production finalize order by toggling isStreaming AFTER replay.
    session_manager.set_streaming(sid, asst_id, False)
    return sid, asst_id


def _run_c3(c1_sid: str, c1_asst_id: str) -> tuple[str, str]:
    """C3: clear msg.events on C1's session in place, then reconcile
    from events.jsonl. Reads what C1 wrote (the scenario-2 backend-
    online/frontend-offline shape — independent writers are out of
    scope here)."""
    from main import _reconcile_msg_events_from_jsonl
    with session_manager.batch(c1_sid):
        sess = session_manager.get(c1_sid)
        msg = next(m for m in sess["messages"] if m["id"] == c1_asst_id)
        msg["events"] = []
        # `sess` is a deepcopy (get() returns a clone), so this
        # mutation doesn't touch the live cache — included
        # nonetheless for hygiene with the post-A' uid_idx invariant.
        msg.pop("_uid_idx", None)
    tree = session_manager.get_root_tree(c1_sid)
    _reconcile_msg_events_from_jsonl(tree)
    return c1_sid, c1_asst_id


# ─── coverage report (the "manual check" surface) ───────────────────


def _count_assistant_text_blocks(jsonl_path: Path) -> int:
    """Sum text blocks across raw assistant lines in one jsonl file."""
    n = 0
    for line in jsonl_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") != "assistant":
            continue
        msg_dict = d.get("message") or {}
        content = msg_dict.get("content")
        if not isinstance(content, list):
            continue
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                n += 1
    return n


def _coverage_report(fixture_path: Path, enriched: list[dict], msg: dict) -> str:
    raw_lines = [l for l in fixture_path.read_text().splitlines() if l.strip()]
    by_raw_type: dict[str, int] = {}
    uuid_present = 0
    for line in raw_lines:
        try:
            d = json.loads(line)
        except Exception:
            by_raw_type["__parse_err__"] = by_raw_type.get("__parse_err__", 0) + 1
            continue
        t = d.get("type", "?")
        by_raw_type[t] = by_raw_type.get(t, 0) + 1
        if d.get("uuid"):
            uuid_present += 1

    # Count raw assistant text blocks across PARENT + every sidecar
    # subagent jsonl — the primary "did we silently drop text?" sanity
    # gate for the eyeball.
    raw_text_blocks = _count_assistant_text_blocks(fixture_path)
    sidecar = fixture_path.parent / fixture_path.stem / "subagents"
    if sidecar.is_dir():
        for sub in sorted(sidecar.glob("agent-*.jsonl")):
            raw_text_blocks += _count_assistant_text_blocks(sub)

    rendered_text_events = 0
    for ev in msg.get("events") or []:
        data = ev.get("data") or {}
        if data.get("type") != "assistant":
            continue
        m = data.get("message") or {}
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                rendered_text_events += 1

    extracted = extract_output_text(msg.get("events") or [])
    rep = []
    rep.append(f"  fixture            = {fixture_path.name}")
    rep.append(f"  raw_lines          = {len(raw_lines)}")
    rep.append(f"  by_raw_type        = {dict(sorted(by_raw_type.items()))}")
    rep.append(f"  raw_uuid_present   = {uuid_present}")
    rep.append(f"  enriched_accepted  = {len(enriched)}  "
               f"(enrich_jsonl_line accepted as event)")
    rep.append(f"  msg_events_after   = {len(msg.get('events') or [])}  "
               f"(post-dedup; uuid-less raw lines silently skipped — OK)")
    rep.append(f"  content_length     = {len(extracted)}")
    rep.append(f"  content_preview    = {extracted[:80]!r}{'…' if len(extracted) > 80 else ''}")
    rep.append(f"  raw_text_blocks    = {raw_text_blocks}")
    rep.append(f"  rendered_text_evts = {rendered_text_events}")
    if raw_text_blocks != rendered_text_events:
        rep.append(f"  {WARN}: raw_text_blocks ({raw_text_blocks}) != "
                   f"rendered_text_evts ({rendered_text_events}) — INVESTIGATE")
    return "\n".join(rep)


# ─── hand-derived invariants per fixture ────────────────────────────
#
# Independent of baseline JSON. These counts are what I traced BY HAND
# from the redacted fixture (parent + every sidecar subagent). They
# lock INGEST CORRECTNESS — not just convergence across callers, but
# that the absolute numbers match an independent reading of the source.
#
# The dedup arithmetic on metadata lines is the most error-prone piece:
# `ai-title` and `file-history-snapshot` go through `_ingest_metadata`,
# which stamps a synthetic uuid from `sha256(data)` — so N raw lines
# with identical data collapse to 1 events.jsonl row. Lines like
# `queue-operation` and `last-prompt` reach the bottom `event_ingester.
# ingest` and write 1 row per raw line (no dedup, no metadata path).
HAND_DERIVED = {
    # 25 parent + 1 sidecar (44 lines). Single Agent call.
    # parent uuid-bearing: 17 (L2 user, L3-4 attachment, L7-17 mix of
    # assistant/user with tool_use+result, L20-22). parent metadata or
    # bookkeeping (no uuid): 2 queue-op, 1 file-hist-snapshot,
    # 3 ai-title (→1 dedup, identical data), 2 last-prompt.
    "with_subagent.jsonl": {
        "render": {
            "msg_events_count": 61,                      # 17 + 44
            "msg_events_by_inner_type": {
                "agent_message:user": 28,                # 7 parent + 21 subagent
                "agent_message:attachment": 2,           # parent L3,L4
                "agent_message:assistant": 31,           # 8 parent + 23 subagent
            },
            "events_with_parent_tool_use_id": 44,        # all subagent
            "unique_parent_tool_use_ids": [
                "toolu_012MYrUCBCxVQBqhxqFZp8PS",
            ],
        },
        "jsonl": {
            "events_jsonl_count": 67,                    # 17 + 2 + 1 + 1 + 2 + 44
            "events_jsonl_inner_type_counts": {
                "user": 28, "attachment": 2, "assistant": 31,
                "queue-operation": 2, "last-prompt": 2,
                "ai-title": 1, "file-history-snapshot": 1,
            },
        },
    },
    # 46 parent + 4 sidecar subagents (4+4+4+9 = 21 lines total).
    # Exercises (1) `_SubagentRegistry.claim` FIFO matching by
    # (agentType, description) across 4 Agent tool_uses + 4 meta files —
    # the alphabetical-filename iteration in `_replay_subagents` means
    # meta files are claimed in filename order, NOT in parent line
    # order; (2) multiple distinct parent_tool_use_id values on subagent
    # events (one per Agent call); (3) a `system` line on the parent
    # (no other fixture has this).
    # parent uuid-bearing: 34 (10 u + 5 att + 18 asst + 1 sys).
    # metadata/bookkeeping: 4 queue-op, 5 ai-title (→1 dedup), 0 fhs,
    # 3 last-prompt.
    "multi_subagent.jsonl": {
        "render": {
            "msg_events_count": 55,                      # 34 + 21
            "msg_events_by_inner_type": {
                "agent_message:user": 16,                # 10 parent + 6 sub
                "agent_message:attachment": 13,          # 5 parent + 8 sub
                "agent_message:assistant": 25,           # 18 parent + 7 sub
                "agent_message:system": 1,
            },
            "events_with_parent_tool_use_id": 21,        # all subagent
            "unique_parent_tool_use_ids": [
                "toolu_014uxkY8phhf6WkQu52LEzon",  # round 4
                "toolu_015bEequsjHCoMy6vS56nmJv",  # round 3
                "toolu_01PfaQdAXVwct6EysX1k7Yiv",  # round 2
                "toolu_01V8qHWTCMTcz1giEKkGKRvj",  # secure remote access
            ],
        },
        "jsonl": {
            "events_jsonl_count": 63,                    # 34 + 4 + 3 + 1 + 21
            "events_jsonl_inner_type_counts": {
                "user": 16, "attachment": 13, "assistant": 25, "system": 1,
                "queue-operation": 4, "last-prompt": 3,
                "ai-title": 1,                           # 5 raw → 1 dedup
            },
        },
    },
    # 46 parent lines, no subagent. 6 thinking blocks across 18
    # assistant lines (highest thinking density of any fixture). Locks
    # that thinking blocks don't break event counting / extraction.
    # parent uuid-bearing: 32 (9 u + 5 att + 18 asst).
    # metadata/bookkeeping: 6 queue-op, 5 ai-title (→1 dedup), 0 fhs,
    # 3 last-prompt.
    "heavy_thinking.jsonl": {
        "render": {
            "msg_events_count": 32,
            "msg_events_by_inner_type": {
                "agent_message:user": 9,
                "agent_message:attachment": 5,
                "agent_message:assistant": 18,
            },
            "events_with_parent_tool_use_id": 0,
            "unique_parent_tool_use_ids": [],
        },
        "jsonl": {
            "events_jsonl_count": 42,                    # 32 + 6 + 3 + 1
            "events_jsonl_inner_type_counts": {
                "user": 9, "attachment": 5, "assistant": 18,
                "queue-operation": 6, "last-prompt": 3,
                "ai-title": 1,
            },
        },
    },
}


def _assert_hand_derived_invariants(
    fixture_name: str, msg: dict, root_id: str,
) -> list[str]:
    """Return list of failure strings (empty list = all pass).
    No-op for fixtures without a hand-derived spec."""
    exp = HAND_DERIVED.get(fixture_name)
    if exp is None:
        return []
    fails: list[str] = []

    events = list(msg.get("events") or [])
    if len(events) != exp["render"]["msg_events_count"]:
        fails.append(
            f"msg.events count: expected {exp['render']['msg_events_count']}, "
            f"got {len(events)}"
        )
    by_inner: dict[str, int] = {}
    for ev in events:
        tag = _classify_event_type(ev)
        by_inner[tag] = by_inner.get(tag, 0) + 1
    if by_inner != exp["render"]["msg_events_by_inner_type"]:
        fails.append(
            f"msg.events by_inner_type: "
            f"expected {exp['render']['msg_events_by_inner_type']}, "
            f"got {by_inner}"
        )
    n_with_ptuid = sum(
        1 for ev in events
        if (ev.get("data") or {}).get("parent_tool_use_id")
    )
    if n_with_ptuid != exp["render"]["events_with_parent_tool_use_id"]:
        fails.append(
            f"events_with_parent_tool_use_id: "
            f"expected {exp['render']['events_with_parent_tool_use_id']}, "
            f"got {n_with_ptuid}"
        )
    unique_ptuids = sorted({
        (ev.get("data") or {}).get("parent_tool_use_id")
        for ev in events
        if (ev.get("data") or {}).get("parent_tool_use_id")
    })
    if unique_ptuids != sorted(exp["render"]["unique_parent_tool_use_ids"]):
        fails.append(
            f"unique parent_tool_use_ids: "
            f"expected {sorted(exp['render']['unique_parent_tool_use_ids'])}, "
            f"got {unique_ptuids}"
        )

    entries = _read_events_jsonl(root_id)
    if len(entries) != exp["jsonl"]["events_jsonl_count"]:
        fails.append(
            f"events.jsonl count: "
            f"expected {exp['jsonl']['events_jsonl_count']}, "
            f"got {len(entries)}"
        )
    inner_counts: dict[str, int] = {}
    for e in entries:
        t = (e.get("data") or {}).get("type", "?")
        inner_counts[t] = inner_counts.get(t, 0) + 1
    if inner_counts != exp["jsonl"]["events_jsonl_inner_type_counts"]:
        fails.append(
            f"events.jsonl by_inner_type: "
            f"expected {exp['jsonl']['events_jsonl_inner_type_counts']}, "
            f"got {inner_counts}"
        )
    return fails


# ─── main test routines ─────────────────────────────────────────────


def _baseline_path(fixture_path: Path) -> Path:
    return fixture_path.with_suffix(".baseline.json")


def _run_all_three_callers(fixture_path: Path) -> dict:
    """Run all three callers; return a results dict.

    Keys:
      render_c1, render_c2, render_c3 — render-tree fingerprints
      jsonl_c1, jsonl_c2 — events.jsonl fingerprints
      c1_sid, c1_asst_id, c1_root — handles for downstream hand-derived
        checks (which need the actual msg + events.jsonl, not just the
        fingerprint).
    """
    c1_sid, c1_asst_id, _enriched = _run_c1(fixture_path)
    c1_root = session_manager._root_id_for(c1_sid)
    c1_sess = session_manager.get(c1_sid)
    c1_msg = next(m for m in c1_sess["messages"] if m["id"] == c1_asst_id)
    render_c1 = _render_fingerprint(c1_msg)
    jsonl_c1 = _jsonl_fingerprint(c1_root)

    c2_sid, c2_asst_id = _run_c2(fixture_path)
    c2_root = session_manager._root_id_for(c2_sid)
    c2_sess = session_manager.get(c2_sid)
    c2_msg = next(m for m in c2_sess["messages"] if m["id"] == c2_asst_id)
    render_c2 = _render_fingerprint(c2_msg)
    jsonl_c2 = _jsonl_fingerprint(c2_root)

    # C3 chains off C1's session — same events.jsonl.
    _run_c3(c1_sid, c1_asst_id)
    c3_sess = session_manager.get(c1_sid)
    c3_msg = next(m for m in c3_sess["messages"] if m["id"] == c1_asst_id)
    render_c3 = _render_fingerprint(c3_msg)

    return {
        "render_c1": render_c1, "render_c2": render_c2, "render_c3": render_c3,
        "jsonl_c1": jsonl_c1, "jsonl_c2": jsonl_c2,
        "c1_sid": c1_sid, "c1_asst_id": c1_asst_id, "c1_root": c1_root,
    }


def _baseline_header() -> dict:
    return {
        "extract_output_text_module": extract_output_text.__module__,
        "version": 1,
    }


def regenerate(accept: bool) -> int:
    rc = 0
    for name in FIXTURE_NAMES:
        path = FIXTURES_DIR / name
        _validate_fixture(path)
        print(f"\n=== {name} ===")
        r = _run_all_three_callers(path)

        # Coverage report — eyeball before accepting.
        c1_sess = session_manager.get(r["c1_sid"])
        c1_msg = next(m for m in c1_sess["messages"] if m["id"] == r["c1_asst_id"])
        enriched = _enrich_all(path)
        print(_coverage_report(path, enriched, c1_msg))

        # Hand-derived correctness check (no-op for fixtures without spec).
        fails = _assert_hand_derived_invariants(name, c1_msg, r["c1_root"])
        if name in HAND_DERIVED:
            if fails:
                print(f"  {WARN}: hand-derived invariants FAILED:")
                for f in fails:
                    print(f"    - {f}")
                rc = 1
            else:
                exp = HAND_DERIVED[name]
                print(
                    f"  {PASS}: hand-derived invariants hold "
                    f"(msg.events={exp['render']['msg_events_count']}, "
                    f"events.jsonl={exp['jsonl']['events_jsonl_count']}, "
                    f"parent_tool_use_id={exp['render']['events_with_parent_tool_use_id']})"
                )

        # Cross-caller convergence (warning during regenerate).
        if r["render_c1"] != r["render_c2"]:
            print(f"  {WARN}: render C1 != C2")
            rc = 1
        if r["render_c1"] != r["render_c3"]:
            print(f"  {WARN}: render C1 != C3")
            rc = 1
        if r["jsonl_c1"] != r["jsonl_c2"]:
            print(f"  {WARN}: jsonl C1 != C2")
            rc = 1

        baseline = {
            "header": _baseline_header(),
            "render": r["render_c1"],
            "jsonl": r["jsonl_c1"],
        }
        if accept:
            _baseline_path(path).write_text(
                json.dumps(baseline, indent=2, sort_keys=True) + "\n"
            )
            print(f"  {PASS}: wrote {_baseline_path(path).name}")
        else:
            print(f"  (dry — re-run with --accept to write baseline)")
    return rc


def assert_against_baseline() -> int:
    rc = 0
    for name in FIXTURE_NAMES:
        path = FIXTURES_DIR / name
        _validate_fixture(path)
        baseline_path = _baseline_path(path)
        if not baseline_path.exists():
            print(f"{FAIL} {name}: no baseline at {baseline_path.name}")
            rc = 1
            continue
        expected = json.loads(baseline_path.read_text())
        # Header sanity: extract_output_text source pin.
        exp_header = expected.get("header") or {}
        got_module = extract_output_text.__module__
        if exp_header.get("extract_output_text_module") != got_module:
            print(
                f"{FAIL} {name}: extract_output_text moved "
                f"({exp_header.get('extract_output_text_module')} → {got_module}); "
                f"bump baseline version + regenerate."
            )
            rc = 1
            continue
        r = _run_all_three_callers(path)
        # Hand-derived correctness check. Runs BEFORE convergence/baseline
        # checks so a hand-derived failure surfaces with its specific diff,
        # not as an opaque fingerprint mismatch. No-op when there's no
        # spec for the fixture.
        if name in HAND_DERIVED:
            c1_sess = session_manager.get(r["c1_sid"])
            c1_msg = next(m for m in c1_sess["messages"] if m["id"] == r["c1_asst_id"])
            fails = _assert_hand_derived_invariants(name, c1_msg, r["c1_root"])
            if fails:
                print(f"{FAIL} {name}: hand-derived invariants")
                for f in fails:
                    print(f"  - {f}")
                rc = 1
                continue
        # Cross-caller convergence.
        if r["render_c1"] != r["render_c2"]:
            print(f"{FAIL} {name}: render C1 != C2 (convergence broken)")
            _diff_dict("  C1 vs C2", r["render_c1"], r["render_c2"])
            rc = 1
            continue
        if r["render_c1"] != r["render_c3"]:
            print(f"{FAIL} {name}: render C1 != C3 (reconcile divergent)")
            _diff_dict("  C1 vs C3", r["render_c1"], r["render_c3"])
            rc = 1
            continue
        if r["jsonl_c1"] != r["jsonl_c2"]:
            print(f"{FAIL} {name}: jsonl C1 != C2")
            _diff_dict("  jsonl C1 vs C2", r["jsonl_c1"], r["jsonl_c2"])
            rc = 1
            continue
        # vs baseline.
        if r["render_c1"] != expected.get("render"):
            print(f"{FAIL} {name}: render fingerprint != baseline")
            _diff_dict("  baseline vs actual", expected.get("render") or {}, r["render_c1"])
            rc = 1
            continue
        if r["jsonl_c1"] != expected.get("jsonl"):
            print(f"{FAIL} {name}: jsonl fingerprint != baseline")
            _diff_dict("  baseline vs actual", expected.get("jsonl") or {}, r["jsonl_c1"])
            rc = 1
            continue
        print(f"{PASS} {name}")
    return rc


def _diff_dict(label: str, a: dict, b: dict) -> None:
    keys = sorted(set(a.keys()) | set(b.keys()))
    print(label)
    for k in keys:
        va, vb = a.get(k), b.get(k)
        if va != vb:
            print(f"    {k}: {va!r} → {vb!r}")


# ─── entrypoint ─────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--regenerate", action="store_true",
                        help="Print per-fixture coverage report (dry).")
    parser.add_argument("--accept", action="store_true",
                        help="With --regenerate: write baseline JSON.")
    args = parser.parse_args()
    try:
        if args.regenerate:
            return regenerate(accept=args.accept)
        return assert_against_baseline()
    finally:
        event_ingester.close_all()
        # Tempdir is leaked intentionally — `mkdtemp` location is
        # documented at startup; manual cleanup if needed.


if __name__ == "__main__":
    sys.exit(main())
