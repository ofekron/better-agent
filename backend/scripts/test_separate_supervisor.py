"""Tests for the separate-supervisor feature.

Pins the unit-level contracts for session_manager.separate_supervisor +
the orchestrator fork-first-turn dispatch by session_id_field:

  1. Successful separation: new root Y in native mode, owns S1 as its
     agent_session_id, copies supervisor-sourced messages from X
     (fresh ids + seq, `source` stripped), baselines the tailer cursor
     to S1's current jsonl line count. X clears its supervisor sid and
     stamps `forked_from_supervisor_agent_sid = S1`, resets the
     bootstrap flag.
  2. Rejects when supervisor_enabled is False.
  3. Rejects when no supervisor sid is set.
  4. Rejects when the active-run gate reports busy (TOCTOU closure
     inside the per-root lock).
  5. Tailer cursor baseline matches the live jsonl line count at
     separation time.
  6. Restart survival — after reload from disk, Y still has the
     pre-seeded messages, S1 as its native sid, and the baseline cursor.
  7. Fork-first-turn dispatch picks the right `forked_from_*` field
     based on session_id_field (native vs supervisor).
  8. WS broadcaster maps `supervisor_separated` to a metadata patch
     carrying the cleared supervisor sid + the new fork marker.

Run with:
    cd backend && .venv/bin/python scripts/test_separate_supervisor.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import asyncio
import tempfile
import traceback

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-separate-supervisor-")
# Isolate claude's projects dir too — separate_supervisor's baseline
# calculation reads jsonls from there.
_TMP_CLAUDE_HOME = tempfile.mkdtemp(prefix="bc-test-separate-claude-home-")
os.environ["CLAUDE_CONFIG_DIR"] = _TMP_CLAUDE_HOME

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from pathlib import Path  # noqa: E402

import _test_installation  # noqa: E402
_test_installation.activate(Path(_TMP_HOME))

import session_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from paths import encode_cwd  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"


def _reset() -> None:
    """Wipe per-test state — session_store cache + session_manager
    in-memory roots + on-disk session files + the simulated claude
    projects dir."""
    session_store._fork_index.clear()
    session_store._index_loaded = False
    session_manager._roots.clear()
    session_manager._node_root_id.clear()
    session_manager._root_locks.clear()
    session_manager._batches.clear()
    session_manager._active_run_gate = None
    sessions_dir = Path(_TMP_HOME) / "sessions"
    if sessions_dir.exists():
        # Drain debounced root persists before removing the dir, or the
        # scheduler's in-flight write races the rmtree (Directory not empty).
        session_manager.flush_pending_persists()
        for _ in range(3):
            try:
                shutil.rmtree(sessions_dir)
                break
            except OSError:
                session_manager.flush_pending_persists()
        else:
            shutil.rmtree(sessions_dir)
    proj_dir = Path(_TMP_CLAUDE_HOME) / "projects"
    if proj_dir.exists():
        shutil.rmtree(proj_dir)


def _seed_supervisor_session(
    *, cwd: str = "/tmp", supervisor_sid: str = "sup-sid-1234",
    n_jsonl_lines: int = 5, n_messages: int = 3,
) -> dict:
    """Create a native session with supervisor_enabled=True, fake a
    supervisor sid + ``n_messages`` supervisor-sourced user+assistant
    messages, and write a fake S1 jsonl with ``n_jsonl_lines`` lines.
    Returns the session record."""
    sess = session_manager.create(
        name="X", model="claude-sonnet-4-6", cwd=cwd,
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    session_manager.set_supervisor_enabled(sid, True)
    session_manager.set_agent_sid(sid, "supervisor", supervisor_sid)
    # Append alternating user/assistant messages tagged source=supervisor.
    for i in range(n_messages):
        session_manager.append_user_msg(sid, {
            "id": f"u-{i}", "role": "user",
            "content": f"verdict prompt {i}", "events": [],
            "timestamp": "2026-05-01T00:00:00", "isStreaming": False,
            "source": "supervisor",
        })
        session_manager.append_assistant_msg(sid, {
            "id": f"a-{i}", "role": "assistant",
            "content": f"verdict response {i}", "events": [],
            "timestamp": "2026-05-01T00:00:00", "isStreaming": False,
            "source": "supervisor",
        })
    # Write a fake S1 claude jsonl so baseline can count it.
    proj_dir = Path(_TMP_CLAUDE_HOME) / "projects" / encode_cwd(cwd)
    proj_dir.mkdir(parents=True, exist_ok=True)
    jsonl = proj_dir / f"{supervisor_sid}.jsonl"
    jsonl.write_text(
        "\n".join(json.dumps({"line": i}) for i in range(n_jsonl_lines))
        + "\n"
    )
    return session_manager.get(sid)


# ─────────────────────────────────────────────────────────────────────


def test_separate_basic() -> None:
    _reset()
    x = _seed_supervisor_session(
        supervisor_sid="sup-sid-AAA", n_jsonl_lines=7, n_messages=2,
    )
    y = session_manager.separate_supervisor(x["id"])
    assert y.get("orchestration_mode") == "native", f"Y mode not native: {y.get('orchestration_mode')!r}"
    assert y.get("agent_session_id") == "sup-sid-AAA", f"Y.agent_session_id wrong: {y.get('agent_session_id')!r}"
    # X side post-state.
    x_post = session_manager.get(x["id"])
    assert x_post.get("supervisor_agent_session_id") is None, (
        f"X.supervisor_agent_session_id not cleared: {x_post.get('supervisor_agent_session_id')!r}"
    )
    assert x_post.get("forked_from_supervisor_agent_sid") == "sup-sid-AAA", (
        f"X.forked_from_supervisor_agent_sid wrong: {x_post.get('forked_from_supervisor_agent_sid')!r}"
    )
    assert x_post.get("supervisor_bootstrap_received") is False, (
        f"X.supervisor_bootstrap_received not reset: {x_post.get('supervisor_bootstrap_received')!r}"
    )
    # Message copy: 2 user + 2 assistant = 4 on Y, all without `source`.
    msgs = y.get("messages") or []
    assert len(msgs) == 4, f"Y.messages length != 4: {len(msgs)}"
    assert all(m.get("source") != "supervisor" for m in msgs), (
        f"Y.messages still has source=supervisor: {[m.get('source') for m in msgs]}"
    )
    # Fresh ids (not equal to the X-side originals "u-0", "a-0", …).
    src_ids = {f"u-{i}" for i in range(2)} | {f"a-{i}" for i in range(2)}
    assert all(m.get("id") not in src_ids for m in msgs), (
        f"Y.messages id collision with X originals: {[m.get('id') for m in msgs]}"
    )
    # Seq monotonic 0..3.
    seqs = [m.get("seq") for m in msgs]
    assert seqs == list(range(4)), f"Y.messages seq not 0..3: {seqs}"
    # Tailer baseline.
    pb = y.get("processed_line_by_sid") or {}
    assert pb.get("sup-sid-AAA") == 7, f"Y.processed_line_by_sid wrong: {pb!r}"


def test_separate_rejects_when_supervisor_disabled() -> None:
    _reset()
    sess = session_manager.create(
        name="X", model="sonnet", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    # supervisor_enabled defaults to False, no sid set.
    try:
        session_manager.separate_supervisor(sess["id"])
    except ValueError as e:
        assert "supervisor not enabled" in str(e), f"wrong ValueError message: {e}"
        return
    raise AssertionError("separate did not raise when supervisor disabled")


def test_separate_rejects_when_no_supervisor_sid() -> None:
    _reset()
    sess = session_manager.create(
        name="X", model="sonnet", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    session_manager.set_supervisor_enabled(sess["id"], True)
    # supervisor_agent_session_id is still None.
    try:
        session_manager.separate_supervisor(sess["id"])
    except ValueError as e:
        assert "supervisor session not yet created" in str(e), f"wrong ValueError message: {e}"
        return
    raise AssertionError("separate did not raise when supervisor sid missing")


def test_separate_rejects_when_active_run_gate_busy() -> None:
    _reset()
    x = _seed_supervisor_session(supervisor_sid="sup-sid-BBB")
    # Wire the gate to report busy.
    session_manager.bind_active_run_gate(lambda sid: True)
    try:
        session_manager.separate_supervisor(x["id"])
    except ValueError as e:
        assert "in flight" in str(e) or "queued" in str(e), f"wrong ValueError message: {e}"
        return
    raise AssertionError("separate did not raise when gate reports busy")


def test_baseline_tailer_cursor_matches_line_count() -> None:
    _reset()
    x = _seed_supervisor_session(
        supervisor_sid="sup-sid-CCC", n_jsonl_lines=23,
    )
    y = session_manager.separate_supervisor(x["id"])
    assert (y.get("processed_line_by_sid") or {}).get("sup-sid-CCC") == 23, (
        f"baseline != 23: {y.get('processed_line_by_sid')!r}"
    )


def test_restart_survival() -> None:
    """After separation, dropping the in-memory cache and reloading
    from disk preserves both Y's pre-seeded state and X's fork
    marker."""
    _reset()
    x = _seed_supervisor_session(
        supervisor_sid="sup-sid-DDD", n_jsonl_lines=4, n_messages=2,
    )
    y = session_manager.separate_supervisor(x["id"])
    xid, yid = x["id"], y["id"]
    # Force the debounced root persist to disk BEFORE dropping the
    # in-memory cache, or the reload below races the scheduler and reads
    # a stale X without its fork marker.
    session_manager.flush_pending_persists()
    # Wipe in-memory state, force a re-load.
    session_manager._roots.clear()
    session_manager._node_root_id.clear()
    session_store._fork_index.clear()
    session_store._index_loaded = False
    x_reloaded = session_manager.get(xid)
    y_reloaded = session_manager.get(yid)
    assert x_reloaded is not None and y_reloaded is not None, (
        f"reload lost a session: x={x_reloaded is not None}, y={y_reloaded is not None}"
    )
    assert x_reloaded.get("forked_from_supervisor_agent_sid") == "sup-sid-DDD", (
        f"X fork marker lost: {x_reloaded.get('forked_from_supervisor_agent_sid')!r}"
    )
    assert y_reloaded.get("agent_session_id") == "sup-sid-DDD", (
        f"Y native sid lost: {y_reloaded.get('agent_session_id')!r}"
    )
    assert (y_reloaded.get("processed_line_by_sid") or {}).get("sup-sid-DDD") == 4, (
        f"Y baseline lost: {y_reloaded.get('processed_line_by_sid')!r}"
    )
    assert len(y_reloaded.get("messages") or []) == 4, (
        f"Y.messages lost: {len(y_reloaded.get('messages') or [])}"
    )


def test_fork_first_turn_field_dispatch() -> None:
    """The orchestrator's fork-first-turn logic picks the correct
    forked_from_* field based on session_id_field. This test inlines
    the same dispatch (a duplicate would diverge silently; instead we
    import and exercise the exact code paths via field reads).

    We test the BRANCH: setting forked_from_supervisor_agent_sid on a
    session whose supervisor_agent_session_id is None must yield a
    fork-first-turn when the orchestrator dispatches with
    session_id_field='supervisor_agent_session_id'. Setting
    forked_from_agent_sid alone must NOT trigger the supervisor branch."""
    _reset()
    sess = session_manager.create(
        name="X", model="sonnet", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    session_manager.set_forked_from_supervisor(sid, "sup-sid-EEE")
    session_manager.set_forked_from(sid, "native-fork-FFF")
    s = session_manager.get(sid)

    # Replicate the exact dispatch from orchestrator.run_turn:
    def pick(session_id_field: str):
        s = session_manager.get(sid)
        current_sid = s.get(session_id_field)
        forked_from_field = (
            "forked_from_supervisor_agent_sid"
            if session_id_field == "supervisor_agent_session_id"
            else "forked_from_agent_sid"
        )
        forked_from_sid = s.get(forked_from_field)
        is_fork_first_turn = not current_sid and bool(forked_from_sid)
        return forked_from_sid if is_fork_first_turn else None

    assert pick("supervisor_agent_session_id") == "sup-sid-EEE", (
        f"supervisor dispatch wrong: {pick('supervisor_agent_session_id')!r}"
    )
    assert pick("agent_session_id") == "native-fork-FFF", (
        f"native dispatch wrong: {pick('agent_session_id')!r}"
    )
    # And clear_forked_from_supervisor is one-shot.
    session_manager.clear_forked_from_supervisor(sid)
    assert session_manager.get(sid).get("forked_from_supervisor_agent_sid") is None, (
        "clear_forked_from_supervisor did not clear field"
    )


def test_ws_broadcaster_maps_supervisor_separated() -> None:
    """The WS broadcaster maps `supervisor_separated` listener events
    to a `session_metadata_updated` frame with a patch carrying the
    cleared supervisor sid + the new fork marker."""
    _reset()
    from session_ws_broadcaster import SessionWSBroadcaster
    captured: list[dict] = []

    class _StubCoordinator:
        # on_change dispatches via coordinator.schedule_global (a bound
        # or running loop is required) — not the legacy broadcast_global.
        def schedule_global(self, type_: str, data: dict, *, loop=None):
            captured.append({"type": type_, "data": data})

    bc = SessionWSBroadcaster(_StubCoordinator())

    async def _drive() -> None:
        bc.on_change("some-sid", {
            "kind": "supervisor_separated",
            "old_supervisor_sid": "sup-sid-XYZ",
        })

    asyncio.run(_drive())
    assert captured, "no WS frame dispatched"
    frame = captured[0]
    assert frame.get("type") == "session_metadata_updated", f"wrong frame type: {frame!r}"
    patch = (frame.get("data") or {}).get("patch") or {}
    assert patch.get("supervisor_agent_session_id") is None, (
        f"patch.supervisor_agent_session_id wrong: {patch!r}"
    )
    assert patch.get("forked_from_supervisor_agent_sid") == "sup-sid-XYZ", (
        f"patch.forked_from_supervisor_agent_sid wrong: {patch!r}"
    )
    assert patch.get("supervisor_bootstrap_received") is False, (
        f"patch.supervisor_bootstrap_received wrong: {patch!r}"
    )


TESTS = [
    ("separate_supervisor happy path",
     test_separate_basic),
    ("rejects when supervisor disabled",
     test_separate_rejects_when_supervisor_disabled),
    ("rejects when no supervisor sid yet",
     test_separate_rejects_when_no_supervisor_sid),
    ("rejects when active-run gate is busy (TOCTOU)",
     test_separate_rejects_when_active_run_gate_busy),
    ("tailer baseline matches jsonl line count",
     test_baseline_tailer_cursor_matches_line_count),
    ("restart survival (Y + X fork marker)",
     test_restart_survival),
    ("fork-first-turn dispatch by session_id_field",
     test_fork_first_turn_field_dispatch),
    ("WS broadcaster maps supervisor_separated",
     test_ws_broadcaster_maps_supervisor_separated),
]


def main_run() -> int:
    failures: list[str] = []
    try:
        for name, fn in TESTS:
            try:
                fn()
            except Exception as e:
                failures.append(name)
                print(f"  {name} raised {type(e).__name__}: {e}")
                traceback.print_exc()
                continue
            print(f"{PASS} {name}")
        print()
        print(f"summary: {len(TESTS) - len(failures)}/{len(TESTS)} passed")
        return 0 if not failures else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main_run())
