from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import sys
if len(sys.argv) >= 3 and sys.argv[1] in {"--read-cursor", "--append-crash", "--reconcile"}:
    os.environ["BETTER_AGENT_HOME"] = sys.argv[2]
else:
    os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(prefix="ba-reconcile-cursor-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from event_ingester import event_ingester
from session_manager import SessionManager
import runtime_ownership


if len(sys.argv) >= 4 and sys.argv[1] == "--read-cursor":
    import hydration_index_store
    root_arg = sys.argv[3]
    print(hydration_index_store.reconcile_cursor(
        root_arg, event_ingester._events_path(root_arg),
    ))
    raise SystemExit(0)

if len(sys.argv) >= 5 and sys.argv[1] == "--append-crash":
    root_arg = sys.argv[3]
    first_value = int(sys.argv[4])
    for _append_value in range(first_value, first_value + 3):
        event_ingester.ingest(
            root_arg, root_arg, "agent_message",
            {"uuid": f"event-{_append_value}", "message": {"role": "assistant", "content": []}},
            source="cursor-restart-test", msg_id="message",
        )
        event_ingester._fsync_dirty_now()
    os._exit(91)

if len(sys.argv) >= 4 and sys.argv[1] == "--reconcile":
    root_arg = sys.argv[3]
    observed: list[int] = []
    child_manager = SessionManager()
    child_manager.bind_reconcile_fn(
        lambda _root, *, after_seq=0: observed.append(after_seq) or [],
    )
    child_manager._sync_reconcile(root_arg)
    print(observed[0] if observed else child_manager._durable_reconcile_cursor(root_arg))
    raise SystemExit(0)


def _append(root: str, value: int) -> None:
    event_ingester.ingest(
        root,
        root,
        "agent_message",
        {"uuid": f"event-{value}", "message": {"role": "assistant", "content": []}},
        source="cursor-restart-test",
        msg_id="message",
    )


def _reconcile_once(
    root: str, *, append_during_reconcile: int | None = None,
    fail_durability: bool = False,
) -> int:
    observed: list[int] = []
    manager = SessionManager()
    def reconcile(_root, *, after_seq=0):
        observed.append(after_seq)
        if append_during_reconcile is not None:
            _append(root, append_during_reconcile)
        return []

    manager.bind_reconcile_fn(reconcile)
    if fail_durability:
        manager.flush_root_persist = lambda _root: (_ for _ in ()).throw(
            OSError("crash before projection durability"),
        )
    try:
        manager._sync_reconcile(root)
    except OSError:
        if not fail_durability:
            raise
    if observed:
        return observed[0]
    return manager._durable_reconcile_cursor(root)


def main() -> int:
    root = "root"
    for value in range(1, 2_001):
        _append(root, value)

    assert _reconcile_once(root) == 0
    assert _reconcile_once(root) == 2_000

    _append(root, 2_001)
    assert _reconcile_once(root, append_during_reconcile=2_002) == 2_000
    assert _reconcile_once(root) == 2_001
    observed_after_race = _reconcile_once(root)
    assert observed_after_race == 2_002, observed_after_race

    _append(root, 2_003)
    assert _reconcile_once(root, fail_durability=True) == 2_002
    assert _reconcile_once(root) == 2_002
    assert _reconcile_once(root) == 2_003

    event_ingester.close_all()
    # One runtime writer per home. Release the parent's writer lock so each
    # crash/restart child below runs as the sole writer (a real sequential
    # restart); the parent re-acquires on its next session write.
    runtime_ownership.release_runtime_writer_lock()
    crashed = subprocess.run(
        [
            sys.executable, __file__, "--append-crash",
            os.environ["BETTER_AGENT_HOME"], root, "2004",
        ],
        check=False,
    )
    assert crashed.returncode == 91
    import hydration_index_store
    _, crash_tail = hydration_index_store.load(root, event_ingester._events_path(root))
    assert crash_tail["cold"] == 0, (
        crash_tail,
        (event_ingester._events_path(root).parent / "event_chain.json").read_text(),
    )
    assert 0 < crash_tail["scanned_bytes"] < event_ingester._events_path(root).stat().st_size
    child = subprocess.run(
        [sys.executable, __file__, "--read-cursor", os.environ["BETTER_AGENT_HOME"], root],
        check=True, capture_output=True, text=True,
    )
    assert child.stdout.strip() == "2003", child.stdout
    reconciled_child = subprocess.run(
        [sys.executable, __file__, "--reconcile", os.environ["BETTER_AGENT_HOME"], root],
        check=True, capture_output=True, text=True,
    )
    assert reconciled_child.stdout.strip() == "2003", reconciled_child.stdout
    final_child = subprocess.run(
        [sys.executable, __file__, "--read-cursor", os.environ["BETTER_AGENT_HOME"], root],
        check=True, capture_output=True, text=True,
    )
    assert final_child.stdout.strip() == "2006", final_child.stdout

    path = event_ingester._events_path(root)
    event_ingester.close(root)
    payload = bytearray(path.read_bytes())
    payload[len(payload) // 2] ^= 1
    path.write_bytes(payload)
    assert _reconcile_once(root) == 0

    print("PASS: reconcile cursor survives restart and resets on rewrite")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        event_ingester.close_all()
        shutil.rmtree(os.environ["BETTER_AGENT_HOME"], ignore_errors=True)
