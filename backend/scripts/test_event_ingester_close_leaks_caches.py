"""Proves review claim #2: `EventIngester.close(root_id)` leaks three
per-root caches. `__init__` creates 10 per-root dicts; `close()`
(event_ingester.py:948-961) pops only 7, leaving `_seen_uids_only`,
`_summaries_cache`, and `_full_scan_cache` keyed by the (now closed)
root forever — an unbounded per-root memory leak in a long-running
backend with many sessions.

All three leaked caches are populated here through real public APIs
(`ingest`, `read_events`, `message_event_summaries`), then `close()`
is called. The test asserts the desired contract — close removes ALL
per-root state — so it FAILS on current code and PASSES once close()
also pops the three leaked dicts.

Run with:
    cd backend && .venv/bin/python scripts/test_event_ingester_close_leaks_caches.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ingester-close-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_ingester import EventIngester  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

ROOT = "root-leak-test"
SID = "sid-leak-test"

_PER_ROOT_STATE_DICTS = [
    "_seq", "_locks", "_seen_uuids", "_seen_uids_only",
    "_max_seq_by_sid", "_seq_offsets", "_next_offset",
    "_summaries_cache", "_full_scan_cache",
]
_PER_ROOT_DICTS = ["_handles", *_PER_ROOT_STATE_DICTS]


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []
    ing = EventIngester()

    # Populate every per-root cache via real public APIs.
    ing.ingest(
        ROOT, sid=SID, event_type="agent_message",
        data={"uuid": "u-1", "type": "assistant",
              "message": {"content": [{"type": "text", "text": "hi"}]}},
        source="test", msg_id="msg-1",
    )
    ing.read_events(ROOT, after_seq=0)          # -> _full_scan_cache
    ing.message_event_summaries(ROOT)           # -> _summaries_cache

    # Sanity: every per-root state cache is warm for ROOT before close.
    warm = [d for d in _PER_ROOT_STATE_DICTS if ROOT in getattr(ing, d)]
    results.append((
        "all per-root state caches warm before close",
        len(warm) == len(_PER_ROOT_STATE_DICTS),
        f"warm={warm}",
    ))

    ing.close(ROOT)

    # Desired contract: close() removes the root from EVERY per-root
    # cache. The three leaked dicts make this FAIL on current code.
    still_present = [d for d in _PER_ROOT_DICTS if ROOT in getattr(ing, d)]
    results.append((
        "close() removes ROOT from every per-root cache",
        still_present == [],
        f"leaked (ROOT still present in): {still_present}",
    ))

    # Pin the exact leak set so a partial fix is still caught.
    expected_leak = {"_seen_uids_only", "_summaries_cache", "_full_scan_cache"}
    results.append((
        "specifically: _seen_uids_only / _summaries_cache / "
        "_full_scan_cache are cleared",
        not (expected_leak & set(still_present)),
        f"these leaked: {sorted(expected_leak & set(still_present))}",
    ))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' — ' + msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def main() -> int:
    try:
        ok = _run()
        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
