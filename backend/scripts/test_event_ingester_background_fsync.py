"""Regression test for backgrounded stable-storage fsync (reqs [26]/[27]).

Locks the contract that `os.fsync()` is NOT on the ingest hot path while
data remains immediately readable:

  1. After `ingest()` / `ingest_batch()` returns, the new line is already
     on disk and parseable (kernel-page-cache visibility via the
     synchronous `fh.flush()`).
  2. No `os.fsync` call is made on the *calling* thread during ingest —
     fsync is deferred to the background flusher. (Catches a regression
     that re-adds sync fsync to the hot path.)
  3. The background flusher clears `_fsync_dirty` within a bounded window.
  4. `close()` drains pending durability synchronously, so an evicted
     handle's events survive a fresh ingester reading the file.

Run with:
    cd backend && .venv/bin/python scripts/test_event_ingester_background_fsync.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ingester-bgfsync-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import event_ingester as ei  # noqa: E402
from event_ingester import EventIngester  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
FSYNC_THREAD_NAME = "event-ingester-fsync"


def _event(uid: str) -> dict:
    return {
        "uuid": uid,
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": uid}]},
    }


def _check(cond: bool, name: str, detail: str = "") -> bool:
    print(f"{PASS if cond else FAIL} {name}{'' if cond else ' -- ' + detail}")
    return cond


def _read_lines(root_id: str) -> list[str]:
    path = os.path.join(_TMP_HOME, "sessions", root_id, "events.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [ln for ln in f.read().splitlines() if ln.strip()]


def _run() -> bool:
    ok = True
    ingester = EventIngester()

    # Record every os.fsync call with the calling thread name.
    calls: list[str] = []
    real_fsync = ei.os.fsync

    def _spy_fsync(fd: int) -> None:
        calls.append(threading.current_thread().name)
        real_fsync(fd)

    ei.os.fsync = _spy_fsync  # type: ignore[attr-defined]
    main_name = threading.current_thread().name
    try:
        # 1+2: single ingest — immediate visibility, no main-thread fsync.
        before = len(calls)
        seq = ingester.ingest(
            "root-a", sid="root-a", event_type="agent_message",
            data=_event("uid-a"), source="test", msg_id="msg-a",
        )
        during = [c for c in calls[before:] if c != FSYNC_THREAD_NAME]
        ok = _check(seq == 1, "ingest returns seq 1", f"{seq=}") and ok
        ok = _check(
            len(_read_lines("root-a")) == 1,
            "ingested line is immediately readable after ingest returns",
            f"lines={_read_lines('root-a')}",
        ) and ok
        ok = _check(
            during == [],
            "no os.fsync on the calling thread during ingest",
            f"main-thread fsync calls={during}",
        ) and ok

        # 1+2: batch ingest — same contract.
        before = len(calls)
        seqs = ingester.ingest_batch(
            "root-b",
            [("root-b", "agent_message", _event("uid-b1"), "test", None, "msg-b1"),
             ("root-b", "agent_message", _event("uid-b2"), "test", None, "msg-b2")],
        )
        during = [c for c in calls[before:] if c != FSYNC_THREAD_NAME]
        ok = _check(seqs == [1, 2], "ingest_batch returns seqs [1,2]", f"{seqs=}") and ok
        ok = _check(
            len(_read_lines("root-b")) == 2,
            "batch lines are immediately readable after ingest_batch returns",
            f"lines={len(_read_lines('root-b'))}",
        ) and ok
        ok = _check(
            during == [],
            "no os.fsync on the calling thread during ingest_batch",
            f"main-thread fsync calls={during}",
        ) and ok

        # 3: background flusher clears the dirty set within a bounded window.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with ingester._fsync_cond:
                dirty_now = set(ingester._fsync_dirty)
            if not dirty_now:
                break
            time.sleep(0.02)
        ok = _check(
            not dirty_now,
            "background flusher clears _fsync_dirty within bounded window",
            f"still dirty={dirty_now}",
        ) and ok
        ok = _check(
            any(c == FSYNC_THREAD_NAME for c in calls),
            "background flusher thread performed the deferred fsync",
            "no fsync from background thread",
        ) and ok

        # 4: close drains durability — a fresh ingester reads every event.
        ingester.close("root-a")
        ingester.close("root-b")
        ok = _check(
            "root-a" not in ingester._fsync_dirty
            and "root-b" not in ingester._fsync_dirty,
            "close discards roots from _fsync_dirty",
        ) and ok

        fresh = EventIngester()
        a_lines = _read_lines("root-a")
        b_lines = _read_lines("root-b")
        ok = _check(len(a_lines) == 1, "root-a events survive close (fresh read)", f"{len(a_lines)}") and ok
        ok = _check(len(b_lines) == 2, "root-b events survive close (fresh read)", f"{len(b_lines)}") and ok
        ok = _check(
            fresh.cursor("root-a") == 1 and fresh.cursor("root-b") == 2,
            "fresh ingester rebuilds seq watermarks from fsync'd file",
            f"a={fresh.cursor('root-a')} b={fresh.cursor('root-b')}",
        ) and ok
        fresh.close_all()

        # 5: singleton reuse after close_all — the flusher must keep
        # working. Regression-locks the bug where `_fsync_dirty_now`
        # permanently killed the flusher on the reused module singleton.
        seq = ingester.ingest(
            "root-c", sid="root-c", event_type="agent_message",
            data=_event("uid-c"), source="test", msg_id="msg-c",
        )
        ok = _check(seq == 1, "ingest works after close_all on same singleton", f"{seq=}") and ok
        ok = _check(
            len(_read_lines("root-c")) == 1,
            "post-close_all ingest line is immediately readable",
        ) and ok
        deadline = time.monotonic() + 5.0
        still_dirty = None
        while time.monotonic() < deadline:
            with ingester._fsync_cond:
                still_dirty = "root-c" in ingester._fsync_dirty
            if not still_dirty:
                break
            time.sleep(0.02)
        ok = _check(
            not still_dirty,
            "flusher still drains after close_all on the reused singleton",
            "root-c never cleared",
        ) and ok
    finally:
        ei.os.fsync = real_fsync  # type: ignore[attr-defined]
        ingester.close_all()

    _ = main_name  # referenced for clarity
    return ok


def main() -> int:
    try:
        return 0 if _run() else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
