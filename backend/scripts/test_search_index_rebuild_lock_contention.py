"""Regression test: rebuild_from_disk() must not stall the hot indexing path.

Bug: `rebuild_from_disk()` held the single module-level `_lock` for its
entire full-corpus disk scan (reading every session's events.jsonl from
disk and batch-inserting all rows). `_apply_rows()` — called by the async
indexing worker on essentially every drained batch of live session-event
text — takes the SAME lock just to do a millisecond-scale sqlite
INSERT/DELETE + commit. While a rebuild ran (schema-version bump,
corruption recovery, or an explicit rebuild), every live indexing write
queued up behind the full-disk rescan, silently stalling search indexing
for however long the rescan took (seconds to minutes on a large session
store) — the same shape of bug fixed twice already this session for
tailer cursor persistence (see cursor_ledger_worker.py and its test).

Fix: `rebuild_from_disk()` now builds the new index into a separate temp
db file WITHOUT holding `_lock` at all. Concurrent `_apply_rows()` calls
keep writing straight through to the live db, and are also buffered
in-memory so the final swap (rename the temp db over the live one) can
replay them onto the fresh db without losing any writes that landed
during the (unlocked) scan. Only that final swap — O(rows written during
the scan), not O(corpus size) — happens under `_lock`.

Two subtests:

  A. Hot-path throughput: while a (patched to be artificially slow)
     rebuild is mid-scan, a burst of `index_event` + `_drain_pending()`
     calls completes fast — proving the hot path is not blocked behind
     the rebuild's `_lock` hold. Before the fix this burst would take as
     long as the remainder of the rebuild.

  B. No data loss: the hot-path rows written *during* the rebuild's scan
     are still searchable once the rebuild completes and swaps in the
     freshly built db — proving the buffer-replay-on-swap logic actually
     preserves concurrent writes rather than discarding them.

Run with:
    cd backend && .venv/bin/python scripts/test_search_index_rebuild_lock_contention.py
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-search-index-rebuild-lock-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from pathlib import Path  # noqa: E402

import session_search_index as ssi  # noqa: E402
import session_store  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_FAKE_FILE_COUNT = 40
_PER_FILE_DELAY = 0.05  # 40 * 0.05s = ~2s total rebuild scan time
_NEEDLE = "rebuildlockcontentionneedle"


def _fake_session_json_files():
    for i in range(_FAKE_FILE_COUNT):
        yield Path(_TMP_HOME) / f"fake-session-{i}.json"


def _make_fake_index_file_rows(midway_event: threading.Event):
    processed = []

    def _fake(sid, fpath):
        time.sleep(_PER_FILE_DELAY)
        processed.append(sid)
        if len(processed) == 5:
            midway_event.set()
        return iter(())

    return _fake


def _hot_entry(text: str) -> dict:
    return {
        "type": "agent_message",
        "data": {"message": {"role": "user", "content": text}},
    }


def main_test() -> bool:
    original_session_json_files = session_store._session_json_files
    original_index_file_rows = ssi._index_file_rows

    midway_event = threading.Event()
    session_store._session_json_files = _fake_session_json_files
    ssi._index_file_rows = _make_fake_index_file_rows(midway_event)

    hot_session_ids = [f"hot-session-{i}" for i in range(30)]

    rebuild_error: list[BaseException] = []

    def _run_rebuild() -> None:
        try:
            ssi.rebuild_from_disk()
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            rebuild_error.append(exc)

    try:
        rebuild_thread = threading.Thread(target=_run_rebuild, name="test-rebuild")
        rebuild_thread.start()

        if not midway_event.wait(timeout=5.0):
            print(f"{FAIL} setup: rebuild never reached midway point")
            rebuild_thread.join(timeout=10.0)
            return False

        start = time.monotonic()
        for sid in hot_session_ids:
            ssi.index_event(sid, _hot_entry(_NEEDLE))
        ssi._drain_pending()
        elapsed = time.monotonic() - start

        rebuild_thread.join(timeout=10.0)
        rebuild_alive = rebuild_thread.is_alive()

        ok_rebuild = not rebuild_alive and not rebuild_error
        print(
            f"{PASS if ok_rebuild else FAIL} setup: rebuild_from_disk finished "
            f"cleanly on its own thread (alive={rebuild_alive}, "
            f"error={rebuild_error[0] if rebuild_error else None})"
        )

        # A: throughput not blocked
        ok_a = elapsed < 1.0 and ok_rebuild
        print(
            f"{PASS if ok_a else FAIL} A: {len(hot_session_ids)} index_event "
            f"+ drain calls completed in {elapsed:.3f}s while rebuild_from_disk "
            f"was mid-scan (want < 1.0s; rebuild scan is ~2.0s total)"
        )

        # B: the swap actually completed (schema version stamped on the
        # renamed-in db) AND no data loss across it. Checking needs_rebuild()
        # matters: if the swap silently failed (e.g. the rebuild thread died
        # before reaching it), the live db would just be whatever
        # _apply_rows lazily created, which never gets PRAGMA user_version
        # set — so this would still read True and catch that failure mode
        # even though the rows below could still be found (written directly
        # by _apply_rows, independent of the swap).
        ssi._close_readonly_connection()
        swap_completed = not ssi.needs_rebuild()
        found = ssi.search(_NEEDLE, limit=100)
        found_ids = {row["session_id"] for row in found}
        missing = set(hot_session_ids) - found_ids
        ok_b = swap_completed and not missing
        print(
            f"{PASS if ok_b else FAIL} B: swap completed={swap_completed}, "
            f"{len(found_ids & set(hot_session_ids))}/{len(hot_session_ids)} "
            f"hot-path rows written during the rebuild scan survived the "
            f"swap (missing: {sorted(missing)[:5]})"
        )

        return ok_a and ok_b
    finally:
        session_store._session_json_files = original_session_json_files
        ssi._index_file_rows = original_index_file_rows


def main() -> int:
    try:
        ok = main_test()
        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
