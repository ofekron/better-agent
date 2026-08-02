"""A3a regression + full branch coverage: WAL infrastructure correctness.

Pins the cross-store Write-Ahead Log contract:

  1. `append(envelope)` durably persists one row; subsequent
     `read_pending()` returns it. Multiple appends preserve order.
  2. `BusEvent.payload` round-trips through `to_json()` / `_parse_line()`.
  3. `WalEnvelope.to_bus_event()` reconstructs a `BusEvent` with
     `is_replay=False` (callers set the flag via `bus.publish`).
  4. `rotate_to_replayed()` moves the live WAL to the rotated path
     atomically; `read_pending()` returns empty after rotation;
     `read_replayed()` returns the rotated content.
  5. `rotate_to_replayed()` MERGES into an existing `.replayed` file
     (crash-mid-replay recovery) and is best-effort on dir fsync failure.
  6. `truncate()` empties the live WAL atomically (and no-ops if absent).
  7. `unlink_replayed()` is idempotent.
  8. Schema-mismatched and non-dict rows raise `WalSchemaError` — no
     auto-migration.
  9. `new_req_id()` produces unique values.
 10. WAL file mode is 0600.

Each test runs against a fresh isolated `BETTER_AGENT_HOME`.
"""

from __future__ import annotations

import json
import stat
import sys

import pytest

# conftest engages test-home protection before this import lands.
import _test_home  # noqa: E402

from event_bus import BusEvent  # noqa: E402
import wal  # noqa: E402
from wal import (  # noqa: E402
    WalEnvelope,
    WalSchemaError,
    _replayed_path,
    _wal_path,
    append,
    iter_replay_envelopes,
    new_req_id,
    read_pending,
    read_replayed,
    rotate_to_replayed,
    truncate,
    unlink_replayed,
)


@pytest.fixture
def home() -> str:
    """Fresh isolated state home per test; wal resolves it lazily."""
    h = _test_home.TestHome.acquire(prefix="bc_wal_")
    yield h.path
    h.release()


def _envelope(
    req_id: str = "req_test",
    *,
    event_type: str = "test.flow",
    seq: int = 0,
    persist: bool = False,
    msg_id: str | None = None,
    run_id: str | None = None,
) -> WalEnvelope:
    return WalEnvelope.from_bus_event(
        req_id=req_id,
        event=BusEvent(
            type=event_type,
            root_id="root-a",
            sid="sid-a",
            payload={"foo": 1, "bar": ["x", "y"]},
            msg_id=msg_id,
            run_id=run_id,
            persist=persist,
            seq=seq,
        ),
    )


# ── append / read_pending ────────────────────────────────────────────────

def test_append_roundtrip_preserves_order_and_mode(home: str) -> None:
    append(_envelope(req_id="req-1"))
    append(_envelope(req_id="req-2"))
    append(_envelope(req_id="req-3"))

    loaded = read_pending()
    assert [e.req_id for e in loaded] == ["req-1", "req-2", "req-3"]
    assert loaded[0].payload == {"foo": 1, "bar": ["x", "y"]}

    mode = stat.S_IMODE(_wal_path().stat().st_mode)
    assert mode == 0o600


def test_read_pending_absent_returns_empty(home: str) -> None:
    assert read_pending() == []


def test_read_pending_skips_blank_lines(home: str) -> None:
    append(_envelope(req_id="req-1"))
    # Inject blank lines between durable rows.
    with _wal_path().open("a", encoding="utf-8") as f:
        f.write("\n   \n")
    append(_envelope(req_id="req-2"))
    assert [e.req_id for e in read_pending()] == ["req-1", "req-2"]


# ── to_bus_event (full-field truthy parse branch) ─────────────────────────

def test_to_bus_event_reconstructs_full_fields(home: str) -> None:
    env = _envelope(
        req_id="req-full",
        seq=7,
        persist=True,
        msg_id="msg-1",
        run_id="run-1",
    )
    append(env)
    rebuilt = read_pending()[0].to_bus_event()

    assert rebuilt.type == "test.flow"
    assert rebuilt.root_id == "root-a"
    assert rebuilt.sid == "sid-a"
    assert rebuilt.payload == {"foo": 1, "bar": ["x", "y"]}
    assert rebuilt.seq == 7
    assert rebuilt.persist is True
    assert rebuilt.msg_id == "msg-1"
    assert rebuilt.run_id == "run-1"
    assert rebuilt.is_replay is False


def test_new_req_id_unique(home: str) -> None:
    ids = {new_req_id() for _ in range(1000)}
    assert len(ids) == 1000
    assert all(i.startswith("req_") for i in ids)


# ── _parse_line error + falsy-default branches ────────────────────────────

def test_parse_non_dict_row_raises(home: str) -> None:
    with _wal_path().open("w", encoding="utf-8") as f:
        f.write(json.dumps([1, 2, 3]) + "\n")
    with pytest.raises(WalSchemaError, match="not a dict"):
        read_pending()


def test_parse_schema_mismatch_raises(home: str) -> None:
    with _wal_path().open("w", encoding="utf-8") as f:
        f.write(json.dumps({
            "schema_version": 99,
            "req_id": "req-bad",
            "ts": "2026-01-01",
            "event_type": "test.bad",
            "root_id": "r",
            "sid": "s",
            "payload": {},
        }) + "\n")
    with pytest.raises(WalSchemaError, match="99"):
        read_pending()


def test_parse_falsy_defaults(home: str) -> None:
    """Missing payload/event_seq/event_persist/msg_id/run_id fall back."""
    with _wal_path().open("w", encoding="utf-8") as f:
        f.write(json.dumps({
            "schema_version": 1,
            "req_id": "req-min",
            "ts": "2026-01-01",
            "event_type": "t",
            "root_id": "r",
            "sid": "s",
        }) + "\n")
    env = read_pending()[0]
    assert env.payload == {}
    assert env.event_seq == 0
    assert env.event_persist is False
    assert env.msg_id is None
    assert env.run_id is None


# ── read_replayed ─────────────────────────────────────────────────────────

def test_read_replayed_absent_returns_empty(home: str) -> None:
    assert read_replayed() == []


def test_read_replayed_roundtrip(home: str) -> None:
    append(_envelope(req_id="req-1"))
    rotate_to_replayed()
    rolled = read_replayed()
    assert [e.req_id for e in rolled] == ["req-1"]


def test_read_replayed_skips_blank_lines(home: str) -> None:
    append(_envelope(req_id="req-1"))
    rotate_to_replayed()
    with _replayed_path().open("a", encoding="utf-8") as f:
        f.write("\n   \n")
    assert [e.req_id for e in read_replayed()] == ["req-1"]


# ── rotate_to_replayed ────────────────────────────────────────────────────

def test_rotate_false_when_no_live_wal(home: str) -> None:
    assert rotate_to_replayed() is False


def test_rotate_moves_atomically(home: str) -> None:
    append(_envelope(req_id="req-1"))
    append(_envelope(req_id="req-2"))

    assert rotate_to_replayed() is True
    assert not _wal_path().exists()
    assert _replayed_path().exists()
    assert [e.req_id for e in read_replayed()] == ["req-1", "req-2"]
    assert read_pending() == []


def test_rotate_merges_into_existing_replayed(home: str) -> None:
    append(_envelope(req_id="req-1"))
    rotate_to_replayed()

    append(_envelope(req_id="req-2"))
    rotate_to_replayed()

    assert [e.req_id for e in read_replayed()] == ["req-1", "req-2"]
    assert not _wal_path().exists()


def test_rotate_dir_fsync_error_is_best_effort(home: str, monkeypatch) -> None:
    """A failed parent-dir fsync (Windows/FUSE) must not abort rotation."""
    append(_envelope(req_id="req-1"))

    parent = str(_wal_path().parent)
    real_open = wal.os.open

    def fake_open(path, flags, *args, **kwargs):
        if str(path) == parent:
            raise OSError("simulated dir open failure")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(wal.os, "open", fake_open)

    assert rotate_to_replayed() is True
    assert not _wal_path().exists()
    assert _replayed_path().exists()


# ── truncate ──────────────────────────────────────────────────────────────

def test_truncate_noop_when_absent(home: str) -> None:
    truncate()  # must not raise
    assert not _wal_path().exists()


def test_truncate_empties_atomically(home: str) -> None:
    append(_envelope(req_id="req-1"))
    assert _wal_path().stat().st_size > 0

    truncate()

    assert _wal_path().exists()
    assert _wal_path().stat().st_size == 0
    assert read_pending() == []


# ── unlink_replayed ───────────────────────────────────────────────────────

def test_unlink_replayed_idempotent(home: str) -> None:
    append(_envelope(req_id="req-1"))
    rotate_to_replayed()
    assert _replayed_path().exists()

    unlink_replayed()
    assert not _replayed_path().exists()

    # Second call hits the FileNotFoundError branch → no-op.
    unlink_replayed()
    assert not _replayed_path().exists()


# ── iter_replay_envelopes ─────────────────────────────────────────────────

def test_iter_replay_envelopes(home: str) -> None:
    assert list(iter_replay_envelopes()) == []

    append(_envelope(req_id="req-1"))
    append(_envelope(req_id="req-2"))
    rotate_to_replayed()

    ids = [e.req_id for e in iter_replay_envelopes()]
    assert ids == ["req-1", "req-2"]


if __name__ == "__main__":
    # Standalone runner for quick local checks; pytest is canonical.
    sys.exit(pytest.main([__file__, "-v"]))
