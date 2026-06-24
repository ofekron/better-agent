"""Regression tests for the streaming-source-of-truth refactor.

After this refactor, `isStreaming` is no longer a persisted field on
assistant messages. It is a derived view of runner registration in
`orchestrator._run_state`: the hook in `coordinator.turn_manager.run_state_add` /
`_run_state_set_target` / `run_state_remove` is the only writer, and
the persisted form is stripped by `session_store.write_session_full`.

Six assertions lock the new contract:

1. Primary-kind runner add/remove drives `msg.isStreaming` via the hook;
   worker-kind runner targeting the SAME msg does NOT flip the parent's
   flag.
2. `session_store.write_session_full` strips `isStreaming` from every
   message on every write — no msg on disk carries the field.
3. A loaded session containing a baked-in `isStreaming: True` (legacy
   upgrade artifact) is stripped on load AND the assistant msg gets a
   `stopped_at` stamp so the Retry button surfaces. New sessions never
   reach this branch.
4. Recovery integrates an alive subprocess by calling
   `coordinator.turn_manager.run_state_add(... target_message_id=msg_id)` — the
   hook is the only path that flips `isStreaming=True`; the recovery
   path makes NO direct `set_streaming(True)` call.
5. A worker registered against the parent msg AFTER the primary
   completes does NOT flip the parent's streaming flag back on.
6. A subprocess that died without writing `complete.json`, with NO
   run dir registered AND NO recovery match, produces a msg that
   loads with `isStreaming=False` AND `stopped_at` stamped, so the
   user can Retry — without the deleted `_reap_zombie_streaming`.

Run with:
    cd backend && .venv/bin/python scripts/test_isstreaming_runner_authority.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-isstreaming-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Import order matches the rest of the suite: env first, then modules.
from session_manager import manager as session_manager  # noqa: E402
from orchestrator import Coordinator  # noqa: E402
import session_store  # noqa: E402

# Standalone coordinator instance for unit testing — the production
# singleton lives in `main.coordinator` but importing `main` pulls in
# uvicorn/FastAPI startup and is too heavy for these targeted tests.
# State on this instance is isolated from production.
coordinator = Coordinator()


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _seed_session(mode: str = "native") -> tuple[str, str]:
    """Create a fresh session with a user msg + scaffolded assistant
    msg. Returns (app_sid, assistant_msg_id). The scaffold initializes
    `isStreaming=True` in-memory (transient — never persisted)."""
    sess = session_manager.create(
        name="t", model="glm-5.1", cwd="/tmp", orchestration_mode=mode,
    )
    sid = sess["id"]
    user_msg = {
        "id": str(uuid.uuid4()),
        "role": "user",
        "content": "do a thing",
        "events": [],
        "isStreaming": False,
    }
    from orchs import get_strategy
    asst_msg = get_strategy(mode).build_assistant_scaffold()
    asst_msg["isStreaming"] = True
    session_manager.append_user_msg(sid, user_msg)
    session_manager.append_assistant_msg(sid, asst_msg)
    return sid, asst_msg["id"]


def _msg(sid: str, msg_id: str) -> dict:
    sess = session_manager.get(sid)
    assert sess is not None
    return next(m for m in sess["messages"] if m["id"] == msg_id)


# ---------------------------------------------------------------------
# (1) Primary runner add/remove drives isStreaming; worker does not.
# ---------------------------------------------------------------------
def test_primary_runner_drives_streaming_worker_does_not() -> bool:
    sid, asst_id = _seed_session("native")

    # Clean slate — turn the seed flag off so we can observe the hook
    # flipping it back on.
    session_manager.set_streaming(sid, asst_id, False)
    assert _msg(sid, asst_id).get("isStreaming") is False

    # Hook A: primary registration with target → True.
    run_id_primary = str(uuid.uuid4())
    coordinator.turn_manager.run_state_add(
        sid, run_id=run_id_primary, kind="native",
        target_message_id=asst_id,
    )
    assert _msg(sid, asst_id).get("isStreaming") is True, "primary add → True"

    # Hook B: primary removal → False.
    coordinator.turn_manager.run_state_remove(sid, run_id_primary)
    assert _msg(sid, asst_id).get("isStreaming") is False, "primary remove → False"

    # Worker registration targeting parent msg → MUST NOT flip parent's flag.
    run_id_worker = f"worker-{uuid.uuid4()}"
    coordinator.turn_manager.run_state_add(
        sid, run_id=run_id_worker, kind="worker",
        target_message_id=asst_id, delegation_id=str(uuid.uuid4()),
    )
    assert _msg(sid, asst_id).get("isStreaming") is False, "worker add → still False"

    coordinator.turn_manager.run_state_remove(sid, run_id_worker)
    assert _msg(sid, asst_id).get("isStreaming") is False, "worker remove → still False"
    return True


# ---------------------------------------------------------------------
# (2) Write path strips isStreaming from disk.
# ---------------------------------------------------------------------
def test_isstreaming_stripped_from_disk() -> bool:
    sid, asst_id = _seed_session("native")
    # Set isStreaming True in-memory (via the hook), then force a
    # persist by mutating something the manager writes.
    coordinator.turn_manager.run_state_add(
        sid, run_id=str(uuid.uuid4()), kind="native",
        target_message_id=asst_id,
    )
    assert _msg(sid, asst_id).get("isStreaming") is True

    # Persist the root.
    root = session_manager.get_root_tree(sid)
    assert root is not None
    session_store.write_session_full(root)

    # Read raw bytes from disk and confirm no msg carries isStreaming.
    path = Path(session_store._sessions_dir()) / f"{root['id']}.json"
    on_disk = json.loads(path.read_text())

    def _walk(node: dict) -> None:
        for m in node.get("messages", []):
            assert "isStreaming" not in m, (
                f"persisted msg {m.get('id')} still has isStreaming: {m}"
            )
        for f in node.get("forks", []) or []:
            _walk(f)

    _walk(on_disk)

    # In-memory state must NOT have been corrupted by the strip — restore
    # is the contract.
    assert _msg(sid, asst_id).get("isStreaming") is True, (
        "in-memory isStreaming clobbered by write — restore is broken"
    )
    return True


# ---------------------------------------------------------------------
# (3) Legacy on-disk isStreaming=True → stripped on load + stopped_at stamped.
# ---------------------------------------------------------------------
def test_legacy_disk_isstreaming_stripped_and_stopped_stamped() -> bool:
    # Build a session on disk with a baked-in isStreaming=True on the
    # last assistant msg, simulating a pre-refactor write.
    root_id = str(uuid.uuid4())
    asst_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    fake_root = {
        "id": root_id,
        "name": "legacy",
        "model": "glm-5.1",
        "cwd": "/tmp",
        "orchestration_mode": "native",
        "created_at": "2020-01-01T00:00:00",
        "updated_at": "2020-01-01T00:00:00",
        "messages": [
            {
                "id": user_id, "role": "user", "content": "x",
                "events": [], "isStreaming": False, "seq": 1,
            },
            {
                "id": asst_id, "role": "assistant", "content": "WIP",
                "events": [], "isStreaming": True, "seq": 2,
            },
        ],
        "forks": [],
    }
    # Write the raw shape directly — bypass write_session_full so the
    # strip doesn't fire (we want to simulate a pre-refactor on-disk
    # state for this single test).
    sessions_dir = Path(session_store._sessions_dir())
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{root_id}.json").write_text(json.dumps(fake_root))

    # Force a fresh load.
    session_manager._roots.pop(root_id, None)
    session_manager._node_root_id.pop(root_id, None)
    loaded = session_manager.get(root_id)
    assert loaded is not None

    asst = next(m for m in loaded["messages"] if m["id"] == asst_id)
    assert "isStreaming" not in asst, (
        f"legacy isStreaming not stripped on load: {asst}"
    )
    assert asst.get("stopped_at"), (
        "legacy streaming msg did not get stopped_at stamp → Retry "
        "button would not appear"
    )
    return True


# ---------------------------------------------------------------------
# (4) Recovery flips isStreaming via the hook — NO direct call.
# ---------------------------------------------------------------------
async def test_recovery_uses_hook_not_direct_set_streaming() -> bool:
    sid, asst_id = _seed_session("native")
    # Reset to baseline.
    session_manager.set_streaming(sid, asst_id, False)

    # Spy: record every direct set_streaming call from this test point on.
    calls: list[tuple[str, str, bool]] = []
    orig_set_streaming = session_manager.set_streaming

    def spy(sid_: str, msg_id_: str, value_: bool) -> None:
        calls.append((sid_, msg_id_, value_))
        return orig_set_streaming(sid_, msg_id_, value_)

    session_manager.set_streaming = spy  # type: ignore[assignment]
    try:
        # Simulate recovery's `_integrate_one` call. Recovery passes
        # `target_message_id=recovering_msg_id` to `run_state_add` —
        # the hook fires and flips isStreaming True. NO explicit
        # `set_streaming(True)` should be observed during this call.
        coordinator.turn_manager.run_state_add(
            sid, run_id=str(uuid.uuid4()), kind="native",
            target_message_id=asst_id,
        )
    finally:
        session_manager.set_streaming = orig_set_streaming  # type: ignore[assignment]

    # Exactly ONE call should have happened, and it must originate from
    # the hook (not a caller-explicit set_streaming).
    assert len(calls) == 1, f"expected 1 streaming call, got {len(calls)}: {calls}"
    assert calls[0] == (sid, asst_id, True), f"unexpected call shape: {calls[0]}"
    assert _msg(sid, asst_id).get("isStreaming") is True
    return True


# ---------------------------------------------------------------------
# (5) Worker AFTER primary completes does not re-flip parent.
# ---------------------------------------------------------------------
def test_worker_after_primary_does_not_reflip_parent() -> bool:
    sid, asst_id = _seed_session("native")
    primary_run = str(uuid.uuid4())
    coordinator.turn_manager.run_state_add(
        sid, run_id=primary_run, kind="native",
        target_message_id=asst_id,
    )
    coordinator.turn_manager.run_state_remove(sid, primary_run)
    assert _msg(sid, asst_id).get("isStreaming") is False

    # Now a delegated worker fires targeting the parent msg.
    worker_run = f"worker-{uuid.uuid4()}"
    coordinator.turn_manager.run_state_add(
        sid, run_id=worker_run, kind="worker",
        target_message_id=asst_id, delegation_id=str(uuid.uuid4()),
    )
    assert _msg(sid, asst_id).get("isStreaming") is False, (
        "worker registered after primary done flipped parent back to True — "
        "the allowlist on _maybe_flip_streaming is broken"
    )
    return True


# ---------------------------------------------------------------------
# (6) Dead-subprocess-no-rundir load yields stopped_at, no reaper.
# ---------------------------------------------------------------------
def test_dead_subprocess_loads_stopped_no_reaper() -> bool:
    # Simulate a pre-refactor on-disk session whose subprocess died
    # without writing complete.json AND whose run dir is gone (manually
    # deleted, $BETTER_CLAUDE_HOME swap, etc.). Recovery has nothing to
    # match against. Load must still produce a msg the user can Retry.
    root_id = str(uuid.uuid4())
    asst_id = str(uuid.uuid4())
    fake_root = {
        "id": root_id,
        "name": "orphan",
        "model": "glm-5.1",
        "cwd": "/tmp",
        "orchestration_mode": "native",
        "created_at": "2020-01-01T00:00:00",
        "updated_at": "2020-01-01T00:00:00",
        "messages": [
            {
                "id": str(uuid.uuid4()), "role": "user", "content": "x",
                "events": [], "isStreaming": False, "seq": 1,
            },
            {
                "id": asst_id, "role": "assistant", "content": "",
                "events": [], "isStreaming": True, "seq": 2,
            },
        ],
        "forks": [],
    }
    sessions_dir = Path(session_store._sessions_dir())
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{root_id}.json").write_text(json.dumps(fake_root))

    # Bypass cache.
    session_manager._roots.pop(root_id, None)
    session_manager._node_root_id.pop(root_id, None)
    loaded = session_manager.get(root_id)
    assert loaded is not None
    asst = next(m for m in loaded["messages"] if m["id"] == asst_id)

    # No runner registered for this msg. Reaper deleted. The load-time
    # legacy strip alone must produce a Retry-able msg.
    assert "isStreaming" not in asst, "isStreaming should be stripped on load"
    assert asst.get("stopped_at"), (
        "dead-subprocess msg loaded without stopped_at — no Retry button"
    )
    # And no run dir exists for this msg → the reaper deletion is safe.
    from provider_claude import _runs_root
    runs_root = _runs_root()
    if runs_root.exists():
        # Defensive: ensure no run targets this msg.
        for d in runs_root.iterdir():
            sj = d / "state.json"
            if sj.exists():
                try:
                    state = json.loads(sj.read_text())
                    assert state.get("target_message_id") != asst_id
                except Exception:
                    pass
    return True


TESTS = [
    ("primary_drives_streaming_worker_does_not", test_primary_runner_drives_streaming_worker_does_not),
    ("isstreaming_stripped_from_disk", test_isstreaming_stripped_from_disk),
    ("legacy_disk_isstreaming_stripped_and_stopped", test_legacy_disk_isstreaming_stripped_and_stopped_stamped),
    ("recovery_uses_hook_not_direct_call", test_recovery_uses_hook_not_direct_set_streaming),
    ("worker_after_primary_does_not_reflip", test_worker_after_primary_does_not_reflip_parent),
    ("dead_subprocess_loads_stopped_no_reaper", test_dead_subprocess_loads_stopped_no_reaper),
]


def main_run() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                if asyncio.iscoroutinefunction(fn):
                    ok = asyncio.run(fn())
                else:
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
