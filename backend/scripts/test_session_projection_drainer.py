from __future__ import annotations

import os
import shutil
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-session-projection-drainer-")

from event_bus_subscribers import SessionProjectionCommand, SessionProjectionDrainer


def _command(root: str, row: dict) -> SessionProjectionCommand:
    return SessionProjectionCommand(
        root_id=root,
        sid=str(row.get("sid") or root),
        msg_id=str(row.get("msg_id") or "m"),
        event_type=str(row["type"]),
        source=str(row.get("source") or "event_bus"),
        seq=int(row["seq"]),
    )


class Harness:
    def __init__(self, rows: dict[str, list[dict]], *, chunk_size: int = 128) -> None:
        self.rows = rows
        self.applied: list[tuple[str, int]] = []
        self.dirty: list[tuple[str, BaseException]] = []
        self.drainer = SessionProjectionDrainer(
            self.apply,
            self.read,
            lambda root, exc: self.dirty.append((root, exc)),
            max_active_roots=16,
            chunk_size=chunk_size,
        )

    def read(self, root: str, after: int, limit: int) -> list[dict]:
        return [row for row in self.rows[root] if int(row["seq"]) > after][:limit]

    def apply(self, root: str, row: dict) -> None:
        self.applied.append((root, int(row["seq"])))


def test_mixed_flood_matches_baseline() -> None:
    root = "flood"
    rows = []
    for seq in range(1, 50_001):
        applicable = seq == 1 or seq % 97 == 0
        rows.append({
            "seq": seq,
            "sid": root,
            "msg_id": "m",
            "type": "agent_message" if applicable else "metadata_only",
            "source": "event_bus" if applicable else "provider_stream",
            "data": {},
        })
    harness = Harness({root: rows})
    try:
        for row in rows:
            harness.drainer.submit(_command(root, row))
        harness.drainer.barrier(root)
        expected = [(root, int(row["seq"])) for row in rows if row["type"] == "agent_message" and row["source"] != "provider_stream"]
        assert harness.applied == expected
        assert not harness.dirty
    finally:
        harness.drainer.shutdown()


def test_target_advance_during_drain_is_not_lost() -> None:
    root = "advance"
    rows = [
        {"seq": seq, "sid": root, "msg_id": "m", "type": "agent_message", "source": "event_bus", "data": {}}
        for seq in range(1, 4)
    ]
    harness = Harness({root: rows}, chunk_size=1)
    entered = threading.Event()
    release = threading.Event()
    original = harness.apply

    def blocked(root_id: str, row: dict) -> None:
        if row["seq"] == 1:
            entered.set()
            assert release.wait(2)
        original(root_id, row)

    harness._apply_row = blocked
    harness.drainer._apply_row = blocked
    try:
        assert harness.drainer.submit(_command(root, rows[0]))
        assert entered.wait(2)
        assert harness.drainer.submit(_command(root, rows[2]))
        release.set()
        harness.drainer.barrier(root)
        assert harness.applied == [(root, 1), (root, 2), (root, 3)]
    finally:
        release.set()
        harness.drainer.shutdown()


def test_failure_does_not_advance_and_retry_succeeds() -> None:
    root = "retry"
    row = {"seq": 1, "sid": root, "msg_id": "m", "type": "agent_message", "source": "event_bus", "data": {}}
    harness = Harness({root: [row]})
    failed = False

    def fail_once(root_id: str, item: dict) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("injected")
        harness.apply(root_id, item)

    harness.drainer._apply_row = fail_once
    try:
        harness.drainer.submit(_command(root, row))
        harness.drainer.barrier(root)
        assert harness.dirty and harness.applied == []
        harness.drainer.submit(_command(root, row))
        harness.drainer.barrier(root)
        assert harness.applied == [(root, 1)]
    finally:
        harness.drainer.shutdown()


def test_command_row_mismatch_both_directions_uses_journal_authority() -> None:
    cases = [
        (
            "expected-apply",
            {"seq": 1, "sid": "expected-apply", "msg_id": "m", "type": "metadata_only", "source": "provider_stream", "data": {}},
            {"event_type": "agent_message", "source": "event_bus"},
            [],
        ),
        (
            "expected-skip",
            {"seq": 1, "sid": "expected-skip", "msg_id": "m", "type": "agent_message", "source": "event_bus", "data": {}},
            {"event_type": "metadata_only", "source": "provider_stream"},
            [("expected-skip", 1)],
        ),
    ]
    for root, row, override, expected in cases:
        harness = Harness({root: [row]})
        command = _command(root, row)
        command = SessionProjectionCommand(
            root_id=command.root_id,
            sid=command.sid,
            msg_id=command.msg_id,
            event_type=override["event_type"],
            source=override["source"],
            seq=command.seq,
        )
        try:
            harness.drainer.submit(command)
            harness.drainer.barrier(root)
            assert harness.applied == expected
            assert len(harness.dirty) == 1
            assert "mismatch" in str(harness.dirty[0][1])
        finally:
            harness.drainer.shutdown()


def test_active_drain_retains_later_coalesced_mismatch_evidence() -> None:
    cases = [
        (
            "active-command-apply",
            {"type": "metadata_only", "source": "provider_stream"},
            {"event_type": "agent_message", "source": "event_bus"},
            [("active-command-apply", 1)],
        ),
        (
            "active-command-skip",
            {"type": "agent_message", "source": "event_bus"},
            {"event_type": "metadata_only", "source": "provider_stream"},
            [("active-command-skip", 1), ("active-command-skip", 2)],
        ),
    ]
    for root, row_two_shape, command_two_shape, expected in cases:
        rows = [
            {"seq": 1, "sid": root, "msg_id": "m", "type": "agent_message", "source": "event_bus", "data": {}},
            {"seq": 2, "sid": root, "msg_id": "m", **row_two_shape, "data": {}},
        ]
        harness = Harness({root: rows}, chunk_size=1)
        entered = threading.Event()
        release = threading.Event()
        original_apply = harness.apply

        def blocked(root_id: str, row: dict) -> None:
            if row["seq"] == 1:
                entered.set()
                assert release.wait(2)
            original_apply(root_id, row)

        harness.drainer._apply_row = blocked
        try:
            harness.drainer.submit(_command(root, rows[0]))
            assert entered.wait(1)
            second = _command(root, rows[1])
            second = SessionProjectionCommand(
                root_id=second.root_id,
                sid=second.sid,
                msg_id=second.msg_id,
                event_type=command_two_shape["event_type"],
                source=command_two_shape["source"],
                seq=second.seq,
            )
            harness.drainer.submit(second)
            release.set()
            harness.drainer.barrier(root)
            assert harness.applied == expected
            assert len(harness.dirty) == 1
            assert "mismatch" in str(harness.dirty[0][1])
        finally:
            release.set()
            harness.drainer.shutdown()


def test_conflicting_duplicate_expectation_is_dirty() -> None:
    root = "duplicate-conflict"
    row = {"seq": 1, "sid": root, "msg_id": "m", "type": "agent_message", "source": "event_bus", "data": {}}
    harness = Harness({root: [row]})
    entered = threading.Event()
    release = threading.Event()
    original_read = harness.drainer._read_rows

    def blocked_read(root_id: str, after: int, limit: int) -> list[dict]:
        entered.set()
        assert release.wait(2)
        return original_read(root_id, after, limit)

    harness.drainer._read_rows = blocked_read
    try:
        harness.drainer.submit(_command(root, row))
        assert entered.wait(1)
        harness.drainer.submit(SessionProjectionCommand(
            root_id=root,
            sid=root,
            msg_id="m",
            event_type="metadata_only",
            source="provider_stream",
            seq=1,
        ))
        assert harness.dirty and "conflicting" in str(harness.dirty[0][1])
        release.set()
        harness.drainer.barrier(root)
        assert harness.applied == [(root, 1)]
    finally:
        release.set()
        harness.drainer.shutdown()


def test_missing_journal_gap_dirties_without_advancing_past_gap() -> None:
    root = "gap"
    rows = [
        {"seq": 1, "sid": root, "msg_id": "m", "type": "agent_message", "source": "event_bus", "data": {}},
        {"seq": 3, "sid": root, "msg_id": "m", "type": "agent_message", "source": "event_bus", "data": {}},
    ]
    harness = Harness({root: rows}, chunk_size=8)
    try:
        harness.drainer.submit(_command(root, rows[0]))
        harness.drainer.barrier(root)
        harness.drainer.submit(_command(root, rows[1]))
        harness.drainer.barrier(root)
        assert harness.applied == [(root, 1)]
        assert harness.dirty and "gap" in str(harness.dirty[-1][1])
    finally:
        harness.drainer.shutdown()


def test_shutdown_is_bounded_and_marks_blocked_root_dirty() -> None:
    root = "shutdown-timeout"
    row = {"seq": 1, "sid": root, "msg_id": "m", "type": "agent_message", "source": "event_bus", "data": {}}
    entered = threading.Event()
    release = threading.Event()
    harness = Harness({root: [row]})
    original_read = harness.drainer._read_rows

    def blocked_read(root_id: str, after: int, limit: int) -> list[dict]:
        entered.set()
        assert release.wait(2)
        return original_read(root_id, after, limit)

    harness.drainer._read_rows = blocked_read
    harness.drainer.submit(_command(root, row))
    assert entered.wait(1)
    started = time.perf_counter()
    harness.drainer.shutdown(timeout_s=0.05)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5
    assert harness.dirty and "shutdown timed out" in str(harness.dirty[-1][1])
    release.set()
    harness.drainer.barrier(root)


def test_unrelated_roots_progress_independently_and_shutdown_barrier() -> None:
    """Each root gets its own dedicated drain thread (KeyedLaneExecutor),
    so a blocked root must never delay an unrelated root's progress --
    not just "eventually before", but concurrently, while still blocked."""
    roots = ["root-0", "root-1"]
    rows = {
        root: [{"seq": seq, "sid": root, "msg_id": "m", "type": "agent_message", "source": "event_bus", "data": {}} for seq in range(1, 5)]
        for root in roots
    }
    harness = Harness(rows, chunk_size=1)
    entered = threading.Event()
    release = threading.Event()
    original = harness.apply

    def blocked(root_id: str, row: dict) -> None:
        if root_id == roots[0] and row["seq"] == 1:
            entered.set()
            assert release.wait(2)
        original(root_id, row)

    harness.drainer._apply_row = blocked
    harness.drainer.submit(_command(roots[0], rows[roots[0]][0]))
    assert entered.wait(2)
    for row in rows[roots[0]][1:]:
        harness.drainer.submit(_command(roots[0], row))
    for row in rows[roots[1]]:
        harness.drainer.submit(_command(roots[1], row))
    # root-1 must fully finish while root-0's first chunk is still
    # blocked -- proving they never share a thread.
    deadline = time.monotonic() + 2
    while (roots[1], 4) not in harness.applied and time.monotonic() < deadline:
        time.sleep(0.005)
    assert (roots[1], 4) in harness.applied, harness.applied
    assert (roots[0], 4) not in harness.applied, harness.applied
    release.set()
    for root in roots:
        harness.drainer.barrier(root)
    harness.drainer.shutdown()
    assert {root for root, _ in harness.applied} == set(roots)
    assert harness.applied.index((roots[1], 1)) < harness.applied.index((roots[0], 4))
    assert harness.drainer.submit(_command(roots[0], rows[roots[0]][-1])) is False


def main() -> int:
    tests = [
        test_mixed_flood_matches_baseline,
        test_target_advance_during_drain_is_not_lost,
        test_failure_does_not_advance_and_retry_succeeds,
        test_command_row_mismatch_both_directions_uses_journal_authority,
        test_active_drain_retains_later_coalesced_mismatch_evidence,
        test_conflicting_duplicate_expectation_is_dirty,
        test_missing_journal_gap_dirties_without_advancing_past_gap,
        test_shutdown_is_bounded_and_marks_blocked_root_dirty,
        test_unrelated_roots_progress_independently_and_shutdown_barrier,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
