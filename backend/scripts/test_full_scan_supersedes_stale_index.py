"""A full summaries scan under the per-root lock is authoritative: it
must REPLACE any stale `_seq_offsets` / `_next_offset`, even when the
fresh index is shorter (file was truncated). A length guard would keep
pre-truncation offsets that `_seq_byte_range` then folds past EOF.

Run with:
    cd backend && .venv/bin/python scripts/test_full_scan_supersedes_stale_index.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-full-scan-supersede-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_ingester import event_ingester  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _check(cond: bool, name: str, detail: str = "") -> bool:
    print(f"{PASS if cond else FAIL} {name}{'' if cond else ' -- ' + detail}")
    return cond


def main() -> int:
    ok = True
    try:
        sid = "s1"
        for i in range(4):
            event_ingester.ingest(
                sid, sid=sid, event_type="agent_message",
                data={"uuid": f"u{i}", "type": "assistant",
                      "message": {"content": []}},
                source="t", run_id=None, msg_id="m1",
            )
        # Warm summaries (full scan populates the real index).
        event_ingester.message_event_summaries(sid)
        real_len = len(event_ingester._seq_offsets[sid])

        # Simulate a stale, too-long index + too-small next_offset, then
        # drop the summaries cache so the next read forces a fresh full
        # scan. (Mirrors a post-truncation read-only cold load where the
        # in-memory index outlived the file.)
        event_ingester._seq_offsets[sid] = (
            event_ingester._seq_offsets[sid] + [10**9, 10**9 + 1]
        )
        event_ingester._next_offset[sid] = 1
        event_ingester._summaries_cache.pop(sid, None)

        event_ingester.message_event_summaries(sid)

        ok = _check(
            len(event_ingester._seq_offsets[sid]) == real_len,
            "full scan replaces stale longer _seq_offsets",
            f"got {len(event_ingester._seq_offsets[sid])}, want {real_len}",
        ) and ok
        ok = _check(
            event_ingester._next_offset[sid] > 1,
            "full scan replaces stale-small _next_offset",
            str(event_ingester._next_offset[sid]),
        ) and ok
        # The bogus high offsets are gone, so no seq maps past EOF.
        bad = event_ingester._seq_byte_range(sid, real_len + 1)
        ok = _check(
            bad is None,
            "_seq_byte_range returns None past the real tail (no EOF overrun)",
            str(bad),
        ) and ok
        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
