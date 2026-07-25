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
    def __init__(
        self,
        rows: dict[str, list[dict]],
        *,
        chunk_size: int = 128,
        max_active_roots: int = 16,
        watermarks: dict[str, int] | None = None,
    ) -> None:
        self.rows = rows
        self.applied: list[tuple[str, int]] = []
        self.applied_watermarks: list[tuple[str, int, int]] = []
        self.dirty: list[tuple[str, BaseException]] = []
        self._watermarks = watermarks or {}
        self.drainer = SessionProjectionDrainer(
            self.apply,
            self.read,
            lambda root, exc: self.dirty.append((root, exc)),
            shards=2,
            max_active_roots=max_active_roots,
            chunk_size=chunk_size,
            get_watermark=lambda root: self._watermarks.get(root, 0),
        )

    def read(self, root: str, after: int, limit: int) -> list[dict]:
        return [row for row in self.rows[root] if int(row["seq"]) > after][:limit]

    def apply(self, root: str, row: dict, fold_start_watermark: int) -> None:
        self.applied.append((root, int(row["seq"])))
        self.applied_watermarks.append((root, int(row["seq"]), fold_start_watermark))


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

    def blocked(root_id: str, row: dict, watermark: int) -> None:
        if row["seq"] == 1:
            entered.set()
            assert release.wait(2)
        original(root_id, row, watermark)

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

    def fail_once(root_id: str, item: dict, watermark: int) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("injected")
        harness.apply(root_id, item, watermark)

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

        def blocked(root_id: str, row: dict, watermark: int) -> None:
            if row["seq"] == 1:
                entered.set()
                assert release.wait(2)
            original_apply(root_id, row, watermark)

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


def test_same_shard_root_progress_and_shutdown_barrier() -> None:
    first = "root-0"
    second = next(
        f"root-{index}"
        for index in range(1, 100)
        if hash(f"root-{index}") % 2 == hash(first) % 2
    )
    roots = [first, second]
    rows = {
        root: [{"seq": seq, "sid": root, "msg_id": "m", "type": "agent_message", "source": "event_bus", "data": {}} for seq in range(1, 5)]
        for root in roots
    }
    harness = Harness(rows, chunk_size=1)
    entered = threading.Event()
    release = threading.Event()
    original = harness.apply

    def blocked(root_id: str, row: dict, watermark: int) -> None:
        if root_id == roots[0] and row["seq"] == 1:
            entered.set()
            assert release.wait(2)
        original(root_id, row, watermark)

    harness.drainer._apply_row = blocked
    harness.drainer.submit(_command(roots[0], rows[roots[0]][0]))
    assert entered.wait(2)
    for row in rows[roots[0]][1:]:
        harness.drainer.submit(_command(roots[0], row))
    for row in rows[roots[1]]:
        harness.drainer.submit(_command(roots[1], row))
    release.set()
    for root in roots:
        harness.drainer.barrier(root)
    harness.drainer.shutdown()
    assert {root for root, _ in harness.applied} == set(roots)
    assert harness.applied.index((roots[1], 1)) < harness.applied.index((roots[0], 4))
    assert harness.drainer.submit(_command(roots[0], rows[roots[0]][-1])) is False


def test_capacity_backpressure_parks_root_and_promotes_on_slot_free() -> None:
    """Backpressure must be lossless: a root over the active-root cap is
    parked (not dropped/dirtied), and gets drained purely because another
    root's slot frees up — never because anything re-reads/re-queries to
    "repair" it. The test issues no repair-triggering call at all besides
    releasing the blocking apply calls that hold the two slots open.
    """
    # roots[0] and roots[1] must land on different shards (each shard is a
    # single-worker executor) so both can genuinely run concurrently and
    # occupy the 2 active-root slots at once; roots[2]'s shard doesn't
    # matter since capacity is a global `_active_roots` count.
    root0 = "cap-0"
    root1 = next(
        f"cap-{index}"
        for index in range(1, 100)
        if hash(f"cap-{index}") % 2 != hash(root0) % 2
    )
    roots = [root0, root1, "cap-parked"]
    rows = {
        root: [
            {"seq": 1, "sid": root, "msg_id": "m", "type": "agent_message", "source": "event_bus", "data": {}},
        ]
        for root in roots
    }
    harness = Harness(rows, chunk_size=8, max_active_roots=2)
    held = {roots[0]: threading.Event(), roots[1]: threading.Event()}
    release = threading.Event()
    original_apply = harness.apply

    def blocked(root_id: str, row: dict, watermark: int) -> None:
        if root_id in held:
            held[root_id].set()
            assert release.wait(2)
        original_apply(root_id, row, watermark)

    harness.drainer._apply_row = blocked
    try:
        assert harness.drainer.submit(_command(roots[0], rows[roots[0]][0])) is True
        assert harness.drainer.submit(_command(roots[1], rows[roots[1]][0])) is True
        assert held[roots[0]].wait(2)
        assert held[roots[1]].wait(2)
        # Both of the only 2 active-root slots are held open by the
        # blocked applies above; a 3rd root must be parked, not dropped.
        assert harness.drainer.submit(_command(roots[2], rows[roots[2]][0])) is True
        assert harness.drainer.active_root_count() == 2
        assert harness.drainer.parked_root_count() == 1
        assert not harness.dirty
        assert harness.applied == []  # nothing dropped/applied early
        # The only thing that happens next: the held applies are released,
        # freeing slots. No read/resubmit/repair call is issued by the test.
        release.set()
        for root in roots:
            harness.drainer.barrier(root)
        assert set(harness.applied) == {(root, 1) for root in roots}
        assert not harness.dirty
        assert harness.drainer.parked_root_count() == 0
    finally:
        release.set()
        harness.drainer.shutdown()


def test_fold_watermark_snapshotted_once_at_activation_not_per_row() -> None:
    """The fold-start watermark must be captured ONCE when a root goes
    idle -> active, not re-read for every row. Proven by mutating the
    watermark source mid-pass (simulating something advancing it, which
    in production only ever happens via `apply_written_journal_event`
    after a row folds -- never mid-read here) and asserting every row in
    THIS pass still sees the value captured at activation."""
    root = "watermark-snapshot"
    rows = [
        {"seq": seq, "sid": root, "msg_id": "m", "type": "agent_message", "source": "event_bus", "data": {}}
        for seq in range(1, 4)
    ]
    harness = Harness({root: rows}, chunk_size=1, watermarks={root: 0})
    original_apply = harness.apply

    def mutate_and_apply(root_id: str, row: dict, watermark: int) -> None:
        original_apply(root_id, row, watermark)
        # Simulate the watermark source advancing mid-pass (e.g. a
        # concurrent process). If the drainer re-read per row instead of
        # snapshotting once, later rows in this SAME pass would observe
        # this mutated value.
        harness._watermarks[root_id] = 999

    harness.drainer._apply_row = mutate_and_apply
    try:
        for row in rows:
            harness.drainer.submit(_command(root, row))
        harness.drainer.barrier(root)
        assert harness.applied == [(root, 1), (root, 2), (root, 3)]
        assert harness.applied_watermarks == [
            (root, 1, 0), (root, 2, 0), (root, 3, 0),
        ]
    finally:
        harness.drainer.shutdown()


def test_cold_start_catchup_judges_all_backlog_rows_against_same_watermark() -> None:
    """A cold-start catch-up of N backlog rows (journaled while the
    process was down) must all be judged against the SAME pre-restart
    watermark -- not against a value that advances as earlier rows in
    the SAME pass are folded."""
    root = "catchup"
    rows = [
        {"seq": seq, "sid": root, "msg_id": "m", "type": "agent_message", "source": "event_bus", "data": {}}
        for seq in range(1, 501)
    ]
    # Pre-restart watermark: the render tree had only seen seq 0 before
    # the backend went down. All 500 backlog rows are "since the
    # watermark" (seq > 0).
    harness = Harness({root: rows}, watermarks={root: 0})
    try:
        for row in rows:
            harness.drainer.submit(_command(root, row))
        harness.drainer.barrier(root)
        assert harness.applied == [(root, seq) for seq in range(1, 501)]
        assert all(watermark == 0 for _, _, watermark in harness.applied_watermarks)
    finally:
        harness.drainer.shutdown()


def main() -> int:
    tests = [
        test_mixed_flood_matches_baseline,
        test_capacity_backpressure_parks_root_and_promotes_on_slot_free,
        test_target_advance_during_drain_is_not_lost,
        test_failure_does_not_advance_and_retry_succeeds,
        test_command_row_mismatch_both_directions_uses_journal_authority,
        test_active_drain_retains_later_coalesced_mismatch_evidence,
        test_conflicting_duplicate_expectation_is_dirty,
        test_missing_journal_gap_dirties_without_advancing_past_gap,
        test_shutdown_is_bounded_and_marks_blocked_root_dirty,
        test_same_shard_root_progress_and_shutdown_barrier,
        test_fold_watermark_snapshotted_once_at_activation_not_per_row,
        test_cold_start_catchup_judges_all_backlog_rows_against_same_watermark,
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
