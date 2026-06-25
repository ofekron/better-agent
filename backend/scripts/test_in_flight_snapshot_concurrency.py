"""Regression test: get_in_flight_assistant_msg snapshot is thread-safe.

The in-flight assistant message is the SAME dict that `apply_event`
mutates — from a different thread (SDK callback / asyncio.to_thread)
under `session_manager._lock_for_root(root_id)`. An unguarded
`copy.deepcopy` walks the dict tree and races a concurrent key change,
raising `RuntimeError: dictionary keys changed during iteration`,
which surfaces to the user as a 500 when entering the assistant
session (websocket_chat → messages_replay → get_in_flight_assistant_msg).

This test reproduces the race with REAL concurrency (no lock mocks):
a mutator thread reshapes the msg dict while holding the same per-root
lock that guards `apply_event`; the main thread hammers the snapshot
reader. Before the fix the reader copied without the lock and crashed;
after the fix it copies under the lock and cannot race.

Run with:
    cd backend && .venv/bin/python scripts/test_in_flight_snapshot_concurrency.py
"""

from __future__ import annotations

import os
import sys
import threading
import time

import _test_home

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
_TMP_HOME = _test_home.isolate("bc-test-inflight-snapshot-")

from session_manager import manager as session_manager  # noqa: E402
from turn_manager import TurnManager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _mk_in_flight_msg() -> tuple[str, str, TurnManager, dict]:
    """Create a session + an in-flight assistant msg registered on a
    TurnManager exactly like run_turn does. Returns (sid, root_id, tm, msg)
    where `msg` is the live dict both the TurnManager and the mutator
    race on."""
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp",
        orchestration_mode="manager", source="cli",
    )
    sid = sess["id"]
    root_id = session_manager._root_id_for(sid)
    assert root_id is not None, "session root must resolve so a per-root lock exists"

    tm = TurnManager(coordinator=None)
    msg = {
        "id": "msg-race-1",
        "role": "assistant",
        "events": [],
        # Large nested dict so deepcopy spends real time iterating keys,
        # widening the window for a concurrent key change to land mid-copy.
        "scratch": {f"k{i}": {"v": i, "buf": [i] * 8} for i in range(400)},
    }
    tm.current_assistant_msgs[sid] = msg
    return sid, root_id, tm, msg


def test_concurrent_snapshot_does_not_crash() -> bool:
    """Mutator thread toggles keys on the msg's nested dict while holding
    the per-root lock (mirroring apply_event under session_manager.batch);
    the main thread deepcopies the in-flight msg via the public reader.
    The pre-fix reader copied unlocked and raised 'dictionary keys changed
    during iteration' within a few hundred iterations."""
    sid, root_id, tm, msg = _mk_in_flight_msg()
    lock = session_manager._lock_for_root(root_id)
    stop = threading.Event()
    errors: list[BaseException] = []

    def mutator() -> None:
        i = 0
        while not stop.is_set():
            with lock:  # same lock apply_event holds while mutating
                scratch = msg["scratch"]
                # Reshape the dict deepcopy is iterating: add + remove keys.
                key = f"hot{i % 16}"
                scratch[key] = {"v": i, "buf": [i] * 16}
                scratch.pop(f"hot{(i + 8) % 16}", None)
                msg[f"_tick{i % 4}"] = i
                msg.pop(f"_tick{(i + 2) % 4}", None)
            i += 1

    t = threading.Thread(target=mutator, daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 5.0
        iterations = 0
        while time.monotonic() < deadline and iterations < 5000:
            snap = tm.get_in_flight_assistant_msg(sid)
            if snap is None:
                errors.append(AssertionError("snapshot unexpectedly None"))
                break
            iterations += 1
    except RuntimeError as exc:
        errors.append(exc)
    finally:
        stop.set()
    t.join(timeout=5.0)
    if t.is_alive():
        errors.append(TimeoutError("mutator thread did not join"))

    if errors:
        for e in errors:
            print(f"      {FAIL} {type(e).__name__}: {e}")
        return False

    # Sanity: the snapshot is an independent deep copy — mutating it must
    # not touch the live msg.
    snap = tm.get_in_flight_assistant_msg(sid)
    snap["scratch"]["LEAKED"] = True
    if "LEAKED" in msg["scratch"]:
        print(f"      {FAIL} snapshot is not a deep copy (mutation leaked)")
        return False

    print(f"      {PASS} {iterations} concurrent snapshots, no RuntimeError")
    return True


# ─── runner ───────────────────────────────────────────────────────

def main() -> int:
    tests = [
        ("test_concurrent_snapshot_does_not_crash", test_concurrent_snapshot_does_not_crash),
    ]
    failed = 0
    for name, fn in tests:
        print(f"  • {name}")
        ok = fn()
        print(f"    {'OK' if ok else 'FAILED'}\n")
        if not ok:
            failed += 1
    if failed:
        print(f"{FAIL} {failed}/{len(tests)} failed")
        return 1
    print(f"{PASS} {len(tests)}/{len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
