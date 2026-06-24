"""Regression test for `_SubagentRegistry.claim` — the type-only
fallback that used to steal pending tool_use_ids was REMOVED.

The bug it caused: during `run_recovery._replay_subagents`, the
sidecar dir on disk accumulates ALL subagent meta files from every
run that ever spawned an Agent against this claude session
(`~/.claude/projects/{cwd}/{claude_sid}/subagents/`). When a recovery
slice's parent jsonl registers only a subset of Agent tool_uses, and
the alphabetically-iterated sidecar dir contains an "extra" meta
whose description doesn't exact-match anything in the slice's
registry, the OLD fallback would grab the first pending entry with
matching `subagent_type` — STEALING a tool_use_id meant for a later
sidecar whose description matched exactly. The actually-matching
sidecar would then find the registry empty, claim() returns None,
and its events are dropped from the replay entirely.

Live evidence from session
`46bafa51-95d2-4f06-a052-de154c8b959c`: `agent-ac5a` ('Adversarial
review of baseline test plan') landed in events.jsonl twice — 19
rows with its correct ptuid + 19 rows mis-attributed to round-4's
`toolu_01Ra…`. `agent-aa37` ('Hostile review round 4') has 0 rows.
13 events lost from that one alone; +40 (a52862) + 13 (ac079) lost
in subsequent turns. 66 events dropped across 3 subagents.

Fix: `claim` returns None on no exact (subagent_type, description)
match. Orphan metas stay unclaimed (skipped) — strictly safer than
mis-attribution + drop.

Run with:
    cd backend && .venv/bin/python scripts/test_subagent_registry_no_fallback_steal.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import uuid as uuid_lib
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-subreg-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from claude_jsonl_enrich import _SubagentRegistry  # noqa: E402
from run_recovery import _replay_subagents, _replay_and_apply  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from event_ingester import event_ingester  # noqa: E402
from orchs import get_strategy  # noqa: E402
from provider_claude import _runs_root  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


# ─── unit tests ───────────────────────────────────────────────────


def test_no_exact_match_returns_none_not_type_fallback() -> bool:
    """Registry with two same-type pending entries; claim with a
    description that matches NEITHER must return None — the removed
    fallback would have popped the first one and returned its id."""
    reg = _SubagentRegistry()
    reg.register("tid-A", "general-purpose", "Round 1")
    reg.register("tid-B", "general-purpose", "Round 2")
    got = reg.claim("general-purpose", "Round 99")
    if got is not None:
        print(f"  claim with unmatched description returned {got!r}; expected None")
        return False
    # Both entries must remain pending (None ≠ pop).
    if len(reg._pending) != 2:
        print(f"  pending size changed: expected 2, got {len(reg._pending)}")
        return False
    return True


def test_exact_match_after_unmatched_attempt() -> bool:
    """After an unmatched claim returns None, an exact-match claim
    later in the iteration MUST still succeed — the fallback removal
    can't break the happy path."""
    reg = _SubagentRegistry()
    reg.register("tid-A", "general-purpose", "Round 1")
    reg.register("tid-B", "general-purpose", "Round 2")
    if reg.claim("general-purpose", "Round 99") is not None:
        return False
    if reg.claim("general-purpose", "Round 1") != "tid-A":
        return False
    if reg.claim("general-purpose", "Round 2") != "tid-B":
        return False
    if reg._pending:
        return False
    return True


def test_subagent_type_mismatch_returns_none() -> bool:
    """A subagent_type that doesn't appear in pending entries — even
    with exact description match on another type — returns None."""
    reg = _SubagentRegistry()
    reg.register("tid-A", "general-purpose", "Find files")
    got = reg.claim("relevant-context-search", "Find files")
    if got is not None:
        print(f"  cross-type match returned {got!r}; expected None")
        return False
    return True


# ─── integration: _replay_subagents over a sidecar dir with one
# unmatched meta ──────────────────────────────────────────────────


def _write_parent_with_agent_call(
    path: Path, agent_uuid: str, agent_tool_use_id: str,
    subagent_type: str, description: str,
) -> None:
    """Write a parent claude jsonl with exactly one Agent tool_use line.
    Shape mirrors what claude SDK emits.
    """
    line = {
        "type": "assistant",
        "uuid": agent_uuid,
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": agent_tool_use_id,
                    "name": "Agent",
                    "input": {
                        "subagent_type": subagent_type,
                        "description": description,
                    },
                },
            ],
        },
    }
    path.write_text(json.dumps(line) + "\n")


def _write_subagent_sidecar(
    sub_dir: Path, agent_id: str, description: str,
    subagent_type: str = "general-purpose", n_lines: int = 3,
) -> None:
    """Drop one meta.json + one agent-X.jsonl into the sidecar dir."""
    sub_dir.mkdir(parents=True, exist_ok=True)
    (sub_dir / f"agent-{agent_id}.meta.json").write_text(json.dumps({
        "agentType": subagent_type,
        "description": description,
        # toolUseId is informational in meta — the registry claim
        # matches by (type, description), not by id.
        "toolUseId": f"toolu_meta_{agent_id}",
    }))
    lines = []
    for i in range(n_lines):
        lines.append(json.dumps({
            "type": "assistant" if i % 2 == 0 else "user",
            "uuid": f"sub-{agent_id}-{i}",
            "message": {"role": "assistant", "content": "..."},
        }))
    (sub_dir / f"agent-{agent_id}.jsonl").write_text("\n".join(lines) + "\n")


def test_replay_subagents_skips_unmatched_meta_no_mis_claim() -> bool:
    """End-to-end: sidecar dir with an EXTRA meta whose description
    isn't in the parent slice's registry. Verify (a) the extra meta
    is SKIPPED (no events from it appear in the replay output),
    (b) the matching meta's events DO appear with the correct
    parent_tool_use_id, (c) NO event from any meta carries a
    parent_tool_use_id meant for a different sidecar.

    Pre-fix behavior (with type-only fallback): the alphabetically-
    earlier extra meta would have stolen the matching meta's
    tool_use_id; the matching meta would find the registry empty
    and get skipped; the extra meta's events would land tagged with
    the STOLEN id, and the matching meta's events would be lost.
    """
    # Use a tempdir to imitate ~/.claude/projects/{cwd}/{sid}.jsonl
    # + <sid>/subagents/agent-*.* layout.
    sandbox = Path(tempfile.mkdtemp(prefix="bc-test-replay-sub-"))
    try:
        sid = "test-claude-sid"
        parent_jsonl = sandbox / f"{sid}.jsonl"
        sub_dir = sandbox / sid / "subagents"

        # Parent slice has ONE Agent tool_use:
        # subagent_type="general-purpose", description="match-B".
        # tool_use_id = "tid-B".
        _write_parent_with_agent_call(
            parent_jsonl,
            agent_uuid="parent-1",
            agent_tool_use_id="tid-B",
            subagent_type="general-purpose",
            description="match-B",
        )

        # Sidecar dir has TWO metas:
        #   agent-aaa.meta.json: description="UNMATCHED-extra"  (orphan from a prior run)
        #   agent-bbb.meta.json: description="match-B"          (the real one for this slice)
        # Alphabetical order makes aaa iterate first — that's where the
        # old fallback would have stolen tid-B.
        _write_subagent_sidecar(sub_dir, "aaa", "UNMATCHED-extra", n_lines=2)
        _write_subagent_sidecar(sub_dir, "bbb", "match-B", n_lines=3)

        # Build a registry seeded from the parent slice (mirrors
        # _replay_from_claude_jsonl's enrichment side-effect).
        registry = _SubagentRegistry()
        registry.register("tid-B", "general-purpose", "match-B")

        # Run the actual subagent replay path, collecting unmatched signals.
        unmatched: list[dict] = []
        out = _replay_subagents(parent_jsonl, registry, unmatched_out=unmatched)

        # Assert (a): aaa's events do NOT appear. Their uuids start "sub-aaa-".
        aaa_uuids = {e["data"].get("uuid") for e in out
                     if e["data"].get("uuid", "").startswith("sub-aaa-")}
        if aaa_uuids:
            print(f"  extra meta 'aaa' was replayed (should have been skipped): {aaa_uuids}")
            return False

        # Assert (b): bbb's events DO appear with parent_tool_use_id=tid-B.
        bbb_events = [e for e in out
                      if e["data"].get("uuid", "").startswith("sub-bbb-")]
        if len(bbb_events) != 3:
            print(f"  matching meta 'bbb' replay: expected 3 events, got {len(bbb_events)}")
            return False
        for e in bbb_events:
            if e["data"].get("parent_tool_use_id") != "tid-B":
                print(f"  bbb event mis-attributed: parent_tool_use_id="
                      f"{e['data'].get('parent_tool_use_id')!r}, expected 'tid-B'")
                return False

        # Assert (c): no event carries an unrelated parent_tool_use_id —
        # specifically, no aaa-shaped event was tagged with tid-B
        # (which would be the mis-claim signature).
        wrong_attribution = [e for e in out
                             if e["data"].get("uuid", "").startswith("sub-aaa-")
                             and e["data"].get("parent_tool_use_id") == "tid-B"]
        if wrong_attribution:
            print(f"  pre-fix mis-claim detected: {len(wrong_attribution)} aaa events stole tid-B")
            return False

        # Assert (d): the unmatched meta (aaa) surfaced as exactly ONE
        # subagent_unmatched signal with correct fields and line_count.
        if len(unmatched) != 1:
            print(f"  expected 1 unmatched signal, got {len(unmatched)}: {unmatched}")
            return False
        sig = unmatched[0]
        if sig.get("type") != "subagent_unmatched":
            print(f"  unmatched signal type wrong: {sig.get('type')!r}")
            return False
        sd = sig.get("data") or {}
        if sd.get("agent_id") != "aaa":
            print(f"  unmatched agent_id wrong: {sd.get('agent_id')!r}")
            return False
        if sd.get("description") != "UNMATCHED-extra":
            print(f"  unmatched description wrong: {sd.get('description')!r}")
            return False
        if sd.get("line_count") != 2:
            print(f"  unmatched line_count wrong: expected 2, got {sd.get('line_count')!r}")
            return False
        if not str(sd.get("uuid", "")).startswith("unmatched-"):
            print(f"  unmatched uuid not synthetic: {sd.get('uuid')!r}")
            return False
        return True
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def test_unmatched_signal_uuid_is_deterministic() -> bool:
    """The synthetic uuid on a subagent_unmatched signal must be stable
    across runs (hash of agent_id + description) so `event_ingester`'s
    uid:sha256(data) dedup collapses the row across repeated recovery
    passes instead of appending a duplicate each time."""
    sandbox = Path(tempfile.mkdtemp(prefix="bc-test-replay-det-"))
    try:
        sid = "det-sid"
        parent_jsonl = sandbox / f"{sid}.jsonl"
        sub_dir = sandbox / sid / "subagents"
        # Parent registers a non-matching Agent so the sole sidecar is unmatched.
        _write_parent_with_agent_call(
            parent_jsonl, agent_uuid="p", agent_tool_use_id="tid-X",
            subagent_type="general-purpose", description="something-else",
        )
        _write_subagent_sidecar(sub_dir, "zzz", "orphan-desc", n_lines=4)

        def _run() -> dict:
            reg = _SubagentRegistry()
            reg.register("tid-X", "general-purpose", "something-else")
            unm: list[dict] = []
            _replay_subagents(parent_jsonl, reg, unmatched_out=unm)
            return unm[0] if unm else {}

        s1 = _run()
        s2 = _run()
        u1 = (s1.get("data") or {}).get("uuid")
        u2 = (s2.get("data") or {}).get("uuid")
        if not u1 or u1 != u2:
            print(f"  uuid not deterministic: {u1!r} vs {u2!r}")
            return False
        return True
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def test_replay_and_apply_emits_orphan_row_with_null_msg_id() -> bool:
    """End-to-end: `_replay_and_apply` over a run_dir whose sidecar dir
    has an unmatched meta must write exactly ONE `subagent_unmatched`
    row to events.jsonl with `msg_id=None` (orphan path via
    `ingest_orphan`), NOT stamped onto the streaming assistant msg.
    Re-running is idempotent (same synthetic uuid → dedup)."""
    sandbox = Path(tempfile.mkdtemp(prefix="bc-test-e2e-orphan-"))
    try:
        # Create a native session + streaming assistant msg.
        sess = session_manager.create(
            name="e2e", model="claude-sonnet", cwd="/tmp",
            orchestration_mode="native",
        )
        app_sid = sess["id"]
        root_id = session_manager._root_id_for(app_sid)
        user_msg = {"id": str(uuid_lib.uuid4()), "role": "user",
                    "content": "go", "events": [], "isStreaming": False}
        asst = get_strategy("native").build_assistant_scaffold()
        session_manager.append_user_msg(app_sid, user_msg)
        session_manager.append_assistant_msg(app_sid, asst)
        asst_id = asst["id"]

        # Build a run_dir: parent jsonl with ONE Agent call that does
        # NOT match the sidecar meta, so the sidecar is unmatched.
        claude_sid = "e2e-claude-sid"
        run_id = str(uuid_lib.uuid4())
        run_dir = _runs_root() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        parent_jsonl = run_dir / "fake_claude" / f"{claude_sid}.jsonl"
        parent_jsonl.parent.mkdir(parents=True, exist_ok=True)
        _write_parent_with_agent_call(
            parent_jsonl, agent_uuid="p1", agent_tool_use_id="tid-NOMATCH",
            subagent_type="general-purpose", description="parent-desc",
        )
        sub_dir = parent_jsonl.parent / parent_jsonl.stem / "subagents"
        _write_subagent_sidecar(sub_dir, "orphanid", "orphan-not-in-parent", n_lines=5)
        (run_dir / "state.json").write_text(json.dumps({
            "run_id": run_id, "mode": "native", "runner_pid": 0,
            "app_session_id": app_sid, "session_id": claude_sid,
            "jsonl_path": str(parent_jsonl), "pre_query_byte_offset": 0,
            "complete": False,
        }))

        sess_live = session_manager.get(app_sid)
        last_asst = next(m for m in sess_live["messages"] if m["id"] == asst_id)

        def _count_orphan_rows() -> tuple[int, set]:
            rows, _, _ = event_ingester.read_events(root_id, limit=1000)
            orphan = [r for r in rows if r.get("type") == "subagent_unmatched"]
            return len(orphan), {r.get("msg_id") for r in orphan}

        with session_manager.batch(app_sid, bump_updated_at=False):
            _replay_and_apply(
                persist_sid=app_sid, run_id=run_id, mode="native",
                claude_sid=claude_sid, sess=sess_live,
                last_asst=last_asst, msg_id=asst_id,
            )
        from event_journal import event_journal_writer
        event_journal_writer.barrier_sync(root_id)

        n, msg_ids = _count_orphan_rows()
        if n != 1:
            print(f"  expected 1 subagent_unmatched row, got {n}")
            return False
        if msg_ids != {None}:
            print(f"  orphan row must have msg_id=None, got msg_ids={msg_ids}")
            return False

        # The orphan event must NOT be on the assistant msg's render tree.
        after = session_manager.get(app_sid)
        a = next(m for m in after["messages"] if m["id"] == asst_id)
        for ev in (a.get("events") or []):
            if (ev.get("data") or {}).get("type") == "subagent_unmatched":
                print("  subagent_unmatched leaked onto msg.events")
                return False

        # Idempotent: re-run yields no new row (deterministic uuid dedup).
        with session_manager.batch(app_sid, bump_updated_at=False):
            _replay_and_apply(
                persist_sid=app_sid, run_id=run_id, mode="native",
                claude_sid=claude_sid, sess=session_manager.get(app_sid),
                last_asst=next(m for m in session_manager.get(app_sid)["messages"]
                               if m["id"] == asst_id),
                msg_id=asst_id,
            )
        event_journal_writer.barrier_sync(root_id)
        n2, _ = _count_orphan_rows()
        if n2 != 1:
            print(f"  re-run not idempotent: expected 1 orphan row, got {n2}")
            return False
        return True
    finally:
        event_ingester.close_all()
        shutil.rmtree(sandbox, ignore_errors=True)


TESTS = [
    ("claim returns None on description mismatch (no type-only fallback)",
        test_no_exact_match_returns_none_not_type_fallback),
    ("happy path: exact-match claims still succeed after one None",
        test_exact_match_after_unmatched_attempt),
    ("cross-subagent-type claim returns None",
        test_subagent_type_mismatch_returns_none),
    ("_replay_subagents skips unmatched meta, no mis-claim, emits signal",
        test_replay_subagents_skips_unmatched_meta_no_mis_claim),
    ("unmatched signal uuid is deterministic (dedup-stable)",
        test_unmatched_signal_uuid_is_deterministic),
    ("_replay_and_apply emits orphan row with msg_id=None, idempotent",
        test_replay_and_apply_emits_orphan_row_with_null_msg_id),
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
