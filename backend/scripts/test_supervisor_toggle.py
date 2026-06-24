"""Tests for the supervisor toggle (no claude CLI subprocesses).

Pins the unit-level contracts:

  1. Toggle on then off — `supervisor_enabled` round-trips via
     `session_manager.set_supervisor_enabled`; broadcast event kind
     is `supervisor_enabled_set`.
  2. Reuse-on-re-enable — `supervisor_agent_session_id` is NOT
     cleared when the toggle is flipped off, so on re-enable the
     existing sid is resumed (verified via direct field check).
  3. Pre-v9 migration — a v4 supervisor-mode session migrates in place
     to v9: orchestration_mode→native, supervisor_enabled=True, the
     worker fork's sid promoted onto the parent's single
     `agent_session_id`, fork dropped. (Existing sessions survive.)
  4. `_v4_to_v5_migrate` is a no-op on an already-v9 record.
  5. Orchestration-mode validation no longer accepts "supervisor"
     for new sessions.

Run with:
    cd backend && .venv/bin/python scripts/test_supervisor_toggle.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import uuid

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-supervisor-toggle-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_toggle_round_trip() -> bool:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    if sess.get("supervisor_enabled") is not False:
        print(f"  default supervisor_enabled != False: {sess.get('supervisor_enabled')!r}")
        return False
    events: list[dict] = []
    session_manager.add_listener(lambda s, c: events.append({"sid": s, **c}))
    session_manager.set_supervisor_enabled(sid, True)
    after_on = session_manager.get(sid)
    if not after_on.get("supervisor_enabled"):
        print(f"  set_supervisor_enabled(True) didn't persist: {after_on.get('supervisor_enabled')!r}")
        return False
    if not any(
        e.get("kind") == "supervisor_enabled_set" and e.get("value") is True
        for e in events
    ):
        print(f"  enabled event not fired: {events!r}")
        return False
    session_manager.set_supervisor_enabled(sid, False)
    after_off = session_manager.get(sid)
    if after_off.get("supervisor_enabled") is not False:
        print(f"  set_supervisor_enabled(False) didn't persist: {after_off!r}")
        return False
    return True


def test_reuse_supervisor_sid_on_re_enable() -> bool:
    """When the toggle is flipped off, `supervisor_agent_session_id` is
    NOT cleared — re-enable resumes the prior supervisor bc session."""
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    fake_sup_sid = "fake-supervisor-sid-1234"
    session_manager.set_supervisor_enabled(sid, True)
    session_manager.set_agent_sid(sid, "supervisor", fake_sup_sid)
    session_manager.set_supervisor_enabled(sid, False)
    after_off = session_manager.get(sid)
    if after_off.get("supervisor_agent_session_id") != fake_sup_sid:
        print(f"  toggle off cleared supervisor sid: {after_off.get('supervisor_agent_session_id')!r}")
        return False
    session_manager.set_supervisor_enabled(sid, True)
    after_on = session_manager.get(sid)
    if after_on.get("supervisor_agent_session_id") != fake_sup_sid:
        print(f"  re-enable lost supervisor sid: {after_on.get('supervisor_agent_session_id')!r}")
        return False
    return True


def test_v4_supervisor_session_migrates() -> bool:
    """A pre-v9 v4 supervisor-mode record migrates in place (no wipe).
    `_v4_to_v5_migrate` promotes the supervisor_worker fork's CLI sid
    onto the parent and flips it to native + supervisor_enabled; the
    v8→v9 step then flattens that onto the single `agent_session_id`."""
    sup_id = str(uuid.uuid4())
    worker_id = str(uuid.uuid4())
    worker_sid = str(uuid.uuid4())
    v4 = {
        "id": sup_id,
        "_schema_version": 4,
        "name": "legacy-sup",
        "model": "claude-sonnet-4-6",
        "cwd": "/tmp",
        "orchestration_mode": "supervisor",
        "manager_agent_session_id": None,
        "native_agent_session_id": None,
        "supervisor_agent_session_id": "sup-sid-abc",
        "kind": "user",
        "messages": [],
        "forks": [
            {
                "id": worker_id,
                "_schema_version": 4,
                "name": f"supervisor-worker:{sup_id[:8]}",
                "model": "claude-sonnet-4-6",
                "cwd": "/tmp",
                "orchestration_mode": "native",
                "manager_agent_session_id": None,
                "native_agent_session_id": worker_sid,
                "supervisor_agent_session_id": None,
                "kind": "supervisor_worker",
                "parent_session_id": sup_id,
                "messages": [],
                "forks": [],
            },
        ],
    }
    path = session_store._sessions_dir() / f"{sup_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(v4))
    session_store._index_loaded = False
    session_store._fork_index.clear()
    session_manager._roots.pop(sup_id, None)
    loaded = session_manager.get(sup_id)
    if loaded is None:
        print("  v4 supervisor record failed to load")
        return False
    ok = True
    if loaded.get("orchestration_mode") != "native":
        print(f"  mode not flipped to native: {loaded.get('orchestration_mode')!r}")
        ok = False
    if not loaded.get("supervisor_enabled"):
        print("  supervisor_enabled not set after migration")
        ok = False
    if loaded.get("agent_session_id") != worker_sid:
        print(f"  worker sid not promoted to agent_session_id: "
              f"{loaded.get('agent_session_id')!r}")
        ok = False
    if "manager_agent_session_id" in loaded or "native_agent_session_id" in loaded:
        print("  old sid fields still present after migration")
        ok = False
    if loaded.get("_schema_version") != session_store.SCHEMA_VERSION:
        print(f"  schema not bumped: {loaded.get('_schema_version')!r}")
        ok = False
    return ok


def test_migration_noop_on_v9() -> bool:
    """Running `_v4_to_v5_migrate` on a record that's already in the
    current v9 shape (no supervisor-mode anywhere) is a no-op — it only
    converts `orchestration_mode == 'supervisor'` records."""
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    before = session_manager.get(sid)
    before_copy = json.loads(json.dumps(before))
    session_store._v4_to_v5_migrate(before, {"dirty": [False]})
    after = session_manager.get(sid) or before
    if before_copy.get("orchestration_mode") != after.get("orchestration_mode"):
        print("  v9 record's mode changed under v4→v5 migration")
        return False
    return True


def test_orchestration_mode_validation_drops_supervisor() -> bool:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp",
        orchestration_mode="supervisor", source="cli",
    )
    if sess.get("orchestration_mode") == "supervisor":
        print("  create_session accepted 'supervisor' mode")
        return False
    return True


TESTS = [
    ("supervisor_enabled toggle round-trip", test_toggle_round_trip),
    ("supervisor sid preserved across off→on cycle", test_reuse_supervisor_sid_on_re_enable),
    ("v4 supervisor-mode session migrates in place to v9", test_v4_supervisor_session_migrates),
    ("_v4_to_v5_migrate is a no-op on a fresh v9 record", test_migration_noop_on_v9),
    ("create_session no longer accepts 'supervisor' mode", test_orchestration_mode_validation_drops_supervisor),
]


def main_run() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                ok = fn()
            except Exception as e:
                ok = False
                print(f"  {name} raised {type(e).__name__}: {e}")
            print(f"{PASS if ok else FAIL} {name}")
            if not ok:
                failed += 1
        print()
        print(f"summary: {len(TESTS) - failed}/{len(TESTS)} passed")
        return 0 if failed == 0 else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main_run())
