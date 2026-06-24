"""A3a regression: WAL infrastructure correctness.

Pins the contract:
  1. `append(envelope)` durably persists one row; subsequent
     `read_pending()` returns it.
  2. Multiple appends produce multiple rows (no clobbering).
  3. `BusEvent.payload` round-trips through `to_json()` /
     `_parse_line()` without mutation.
  4. `WalEnvelope.to_bus_event()` reconstructs a `BusEvent` with
     `is_replay=False` (callers set the flag via `bus.publish`).
  5. `rotate_to_replayed()` moves the live WAL to the rotated path
     atomically; `read_pending()` returns empty after rotation;
     `read_replayed()` returns the rotated content.
  6. `rotate_to_replayed()` MERGES into an existing `.replayed`
     file (crash-mid-replay recovery: the rotated file is the
     authority during replay; a crash mid-replay must not lose
     envelopes that were already in `.replayed`).
  7. `truncate()` empties the live WAL atomically.
  8. `unlink_replayed()` is idempotent.
  9. Schema-mismatched rows raise `WalSchemaError` — no auto-migration.
 10. `new_req_id()` produces unique values.
 11. WAL file mode is 0600 (per CLAUDE.md isolation rule + the
     internal_token discipline — no user-other-readable state).

Run with:
    cd backend && .venv/bin/python scripts/test_wal_infrastructure.py
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Use a tempdir for the whole test (and set the env BEFORE importing wal).
import _test_home
_TMP = _test_home.isolate("bc_a3a_")

from event_bus import BusEvent  # noqa: E402
from wal import (  # noqa: E402
    WalEnvelope,
    WalSchemaError,
    _wal_path,
    _replayed_path,
    append,
    new_req_id,
    read_pending,
    read_replayed,
    rotate_to_replayed,
    truncate,
    unlink_replayed,
)


def _make_envelope(req_id: str = "req_test", event_type: str = "test.flow") -> WalEnvelope:
    return WalEnvelope.from_bus_event(
        req_id=req_id,
        event=BusEvent(
            type=event_type,
            root_id="root-a",
            sid="sid-a",
            payload={"foo": 1, "bar": ["x", "y"]},
            persist=False,
        ),
    )


def _check(cond: bool, label: str, failures: list[str]) -> None:
    print(f"  {'OK' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def main_entry() -> int:
    failures: list[str] = []

    try:
        # ── 1. append + read_pending round-trip ─────────────────────
        env1 = _make_envelope(req_id="req-1")
        append(env1)
        loaded = read_pending()
        _check(
            len(loaded) == 1 and loaded[0].req_id == "req-1",
            "single append → read_pending returns the envelope",
            failures,
        )

        # ── 2. multiple appends ─────────────────────────────────────
        env2 = _make_envelope(req_id="req-2")
        env3 = _make_envelope(req_id="req-3")
        append(env2)
        append(env3)
        loaded = read_pending()
        ids = [e.req_id for e in loaded]
        _check(
            ids == ["req-1", "req-2", "req-3"],
            f"3 appends preserve order (got {ids})",
            failures,
        )

        # ── 3. payload round-trips ──────────────────────────────────
        _check(
            loaded[0].payload == {"foo": 1, "bar": ["x", "y"]},
            "payload round-trips through to_json / _parse_line",
            failures,
        )

        # ── 4. to_bus_event reconstruction ──────────────────────────
        rebuilt = loaded[0].to_bus_event()
        _check(
            rebuilt.type == "test.flow"
            and rebuilt.root_id == "root-a"
            and rebuilt.payload == {"foo": 1, "bar": ["x", "y"]}
            and rebuilt.is_replay is False,
            "to_bus_event reconstructs the BusEvent with is_replay=False",
            failures,
        )

        # ── 11. WAL mode is 0600 ────────────────────────────────────
        mode = stat.S_IMODE(_wal_path().stat().st_mode)
        _check(
            mode == 0o600,
            f"WAL file mode is 0600 (got {oct(mode)})",
            failures,
        )

        # ── 5. rotate_to_replayed atomic move ───────────────────────
        moved = rotate_to_replayed()
        _check(moved is True, "rotate_to_replayed returns True when live WAL existed", failures)
        _check(not _wal_path().exists(), "live WAL gone after rotation", failures)
        rolled = read_replayed()
        rolled_ids = [e.req_id for e in rolled]
        _check(
            rolled_ids == ["req-1", "req-2", "req-3"],
            f"rotated file holds the original 3 envelopes (got {rolled_ids})",
            failures,
        )

        # ── 6. rotate MERGES into existing .replayed ────────────────
        env4 = _make_envelope(req_id="req-4")
        append(env4)
        rotate_to_replayed()
        rolled = read_replayed()
        rolled_ids = [e.req_id for e in rolled]
        _check(
            rolled_ids == ["req-1", "req-2", "req-3", "req-4"],
            f"second rotation merges into existing .replayed "
            f"(got {rolled_ids})",
            failures,
        )

        # ── 7. truncate empties live WAL atomically ─────────────────
        env5 = _make_envelope(req_id="req-5")
        append(env5)
        _check(_wal_path().exists(), "live WAL exists before truncate", failures)
        truncate()
        _check(
            _wal_path().exists() and _wal_path().stat().st_size == 0,
            "truncate leaves an empty live WAL (not deleted)",
            failures,
        )
        _check(
            read_pending() == [],
            "read_pending returns [] after truncate",
            failures,
        )

        # ── 8. unlink_replayed idempotent ───────────────────────────
        _check(_replayed_path().exists(), "rotated file exists before unlink_replayed", failures)
        unlink_replayed()
        _check(not _replayed_path().exists(), "rotated file gone after unlink_replayed", failures)
        unlink_replayed()  # second call should be no-op
        _check(not _replayed_path().exists(), "second unlink_replayed is idempotent", failures)

        # ── 9. schema mismatch raises WalSchemaError ────────────────
        bad_path = _wal_path()
        with bad_path.open("w", encoding="utf-8") as f:
            f.write(json.dumps({
                "schema_version": 99,
                "req_id": "req-bad",
                "ts": "2026-01-01",
                "event_type": "test.bad",
                "root_id": "r",
                "sid": "s",
                "payload": {},
            }) + "\n")
        raised = False
        try:
            read_pending()
        except WalSchemaError as exc:
            raised = True
            _check(
                "99" in str(exc) and "no auto-migration" in str(exc),
                "WalSchemaError details version + wipe instruction",
                failures,
            )
        _check(raised, "schema-mismatched row raises WalSchemaError", failures)
        bad_path.unlink()

        # ── 10. new_req_id uniqueness ───────────────────────────────
        ids = {new_req_id() for _ in range(1000)}
        _check(len(ids) == 1000, "new_req_id produces unique values", failures)

    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

    if failures:
        print(f"\n{len(failures)} FAILURES")
        return 1
    print("\nall A3a checks OK")
    return 0


if __name__ == "__main__":
    sys.exit(main_entry())
