"""Unit coverage for the read-side cluster of event_ingester.py:
read_events (missing-file / offset fast-path / full-scan cache hit / extend /
cold-scan populate / in-memory filter loop), _scan_from (blank-line skip /
sid+msg_id filters / early-exit / cold populate + write-cache seeding /
unlocked catch-up append), read_ws_events (manager_event unwrap + else shape),
and read_ws_events_range (missing file / byte range / unwrap / blank + malformed
skip / EOF).

Run with:
    cd backend && .venv/bin/python scripts/test_event_ingester_read_path.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ingester-read-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_ingester import EventIngester  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _check(cond: bool, name: str, detail: str = "") -> bool:
    print(f"{PASS if cond else FAIL} {name}{'' if cond else ' -- ' + detail}")
    return cond


def _agent(uid: str, text: str = "hi") -> dict:
    return {"uuid": uid, "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]}}


def _events_path(root_id: str) -> str:
    return os.path.join(_TMP_HOME, "sessions", root_id, "events.jsonl")


def _append_raw(root_id: str, entry: dict) -> tuple[int, int]:
    """Append a hand-built row directly; return (line_start, line_end) bytes."""
    path = _events_path(root_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    start = os.path.getsize(path) if os.path.exists(path) else 0
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
    return start, start + len(line.encode("utf-8"))


def _append_raw_text(root_id: str, text: str) -> int:
    """Append an arbitrary raw line (may be blank/malformed); return start byte."""
    path = _events_path(root_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    start = os.path.getsize(path) if os.path.exists(path) else 0
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    return start


def _fresh() -> EventIngester:
    return EventIngester()


# --------------------------------------------------------------------------- #
# read_events: missing file
# --------------------------------------------------------------------------- #
def test_read_events_missing_file() -> bool:
    ing = _fresh()
    out, total, has_more = ing.read_events("never-existed")
    return _check((out, total, has_more) == ([], 0, False),
                  "missing file -> ([], 0, False)",
                  f"{out!r} {total} {has_more}")


# --------------------------------------------------------------------------- #
# read_events: offset fast-path (after_seq>0 with populated _seq_offsets)
# --------------------------------------------------------------------------- #
def test_read_events_offset_fast_path() -> bool:
    ok = True
    ing = _fresh()
    root, sid = "read-fast", "sid-A"
    for uid in ("u1", "u2", "u3"):
        ing.ingest(root, sid=sid, event_type="agent_message",
                   data=_agent(uid), source="test", msg_id="m1")
    # Fast path returns rows strictly after after_seq=1 -> seq 2,3.
    out, total, has_more = ing.read_events(root, after_seq=1, limit=500)
    seqs = [e["seq"] for e in out]
    ok = _check(seqs == [2, 3], "fast path after_seq=1 -> seq 2,3", f"{seqs}") and ok
    ok = _check(total == 2 and has_more is False, "total/has_more", f"{total} {has_more}") and ok

    # after_seq beyond the offset index -> empty.
    out2, total2, has_more2 = ing.read_events(root, after_seq=10, limit=500)
    ok = _check((out2, total2, has_more2) == ([], 0, False),
                "after_seq beyond range -> ([], 0, False)",
                f"{out2} {total2} {has_more2}") and ok

    # Fast path ALSO threads sid_filter / msg_id_filter into _scan_from
    # (the offset path filters at the read layer, not in memory).
    out_s, total_s, _ = ing.read_events(root, after_seq=1, sid_filter="other")
    ok = _check(total_s == 0, "fast-path sid_filter mismatch drops all", f"{total_s}") and ok
    out_m, total_m, _ = ing.read_events(root, after_seq=1, msg_id_filter="other")
    ok = _check(total_m == 0, "fast-path msg_id_filter mismatch drops all", f"{total_m}") and ok
    return ok


# --------------------------------------------------------------------------- #
# read_events: cold full-scan populate (offsets None) + write-cache seeding
# --------------------------------------------------------------------------- #
def test_read_events_cold_scan_populate() -> bool:
    ok = True
    # A fresh ingester that never ingested -> _seq_offsets is None, so the
    # cold-scan path runs _scan_from(populate_cache=True) and seeds the
    # write caches from a clean (no trailing-invalid) file.
    ing = _fresh()
    root = "read-cold"
    for i in range(3):
        _append_raw(root, {"seq": i + 1, "ts": "t", "sid": "sid-A",
                           "type": "agent_message", "data": _agent(f"c{i}"),
                           "source": "test", "msg_id": "m1"})
    out, total, has_more = ing.read_events(root, after_seq=0, limit=500)
    ok = _check(total == 3, "cold scan sees all 3 rows", f"{total}") and ok
    ok = _check([e["seq"] for e in out] == [1, 2, 3], "cold scan order", f"{out}") and ok
    # Cold populate must have installed the offset index + write caches.
    ok = _check(ing._seq_offsets.get(root) is not None,
                "cold scan populated _seq_offsets") and ok
    ok = _check(ing.current_seq(root) == 3,
                "cold scan seeded current_seq == parsed line count",
                f"{ing.current_seq(root)}") and ok
    ok = _check(root in ing._seen_uuids,
                "cold scan seeded write dedup caches") and ok
    return ok


# --------------------------------------------------------------------------- #
# read_events cold scan: a malformed trailing line leaves trailing_invalid
# True, so write-cache seeding is correctly SKIPPED (the seed must only run
# on a clean file whose identity matches a full, valid scan).
# --------------------------------------------------------------------------- #
def test_read_events_cold_scan_trailing_malformed_skips_seed() -> bool:
    ok = True
    ing = _fresh()
    root = "read-cold-malformed"
    _append_raw(root, {"seq": 1, "ts": "t", "sid": "sid-A", "type": "agent_message",
                       "data": _agent("c0"), "source": "test", "msg_id": "m1"})
    _append_raw_text(root, "{broken\n")  # trailing malformed line
    out, total, _ = ing.read_events(root)
    ok = _check(total == 1, "valid row still read despite trailing malformed", f"{total}") and ok
    # Seeding skipped: write-caches untouched (current_seq stays unset).
    ok = _check(ing.current_seq(root) is None,
                "malformed tail skips write-cache seeding (current_seq None)",
                f"{ing.current_seq(root)}") and ok
    ok = _check(root not in ing._seen_uuids,
                "malformed tail skips dedup-cache seeding") and ok
    # Offset index still installed for the parsed lines.
    ok = _check(ing._seq_offsets.get(root) is not None,
                "offset index still populated for parsed lines") and ok
    return ok


# --------------------------------------------------------------------------- #
# read_events cold scan: the in-memory after_seq filter (only reachable on
# a COLD scan with after_seq>0; once offsets are populated the offset fast
# path takes over and the loop is never entered for paging).
# --------------------------------------------------------------------------- #
def test_read_events_cold_after_seq_filter() -> bool:
    ok = True
    ing = _fresh()
    root = "read-cold-after"
    for i in range(3):
        _append_raw(root, {"seq": i + 1, "ts": "t", "sid": "sid-A",
                           "type": "agent_message", "data": _agent(f"d{i}"),
                           "source": "test", "msg_id": "m1"})
    # offsets is None on this fresh ingester -> cold path -> in-memory loop
    # applies the after_seq filter.
    out, total, _ = ing.read_events(root, after_seq=1)
    ok = _check(total == 2, "cold in-memory after_seq=1 keeps seq 2,3", f"{total}") and ok
    ok = _check([e["seq"] for e in out] == [2, 3], "cold after_seq order", f"{out}") and ok
    return ok


# --------------------------------------------------------------------------- #
# read_events: full-scan cache hit (same file size) + extend (file grew)
# --------------------------------------------------------------------------- #
def test_read_events_cache_hit_and_extend() -> bool:
    ok = True
    ing = _fresh()
    root, sid = "read-cache", "sid-A"
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent("h1"), source="test", msg_id="m1")
    # First read builds the full-scan cache.
    ing.read_events(root)
    cached = ing._full_scan_cache.get(root)
    ok = _check(cached is not None, "full-scan cache populated") and ok
    # Second read with unchanged size -> cache-hit branch (no rescan).
    out, _, _ = ing.read_events(root)
    ok = _check(len(out) == 1, "cache hit returns the row", f"{len(out)}") and ok

    # External append grows the file -> extend branch picks up the new row.
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent("h2"), source="test", msg_id="m1")
    out2, total2, _ = ing.read_events(root)
    ok = _check(total2 == 2, "extend picks up appended row", f"{total2}") and ok
    return ok


# --------------------------------------------------------------------------- #
# read_events: in-memory filter loop (sid / msg_id / after_seq / has_more)
# --------------------------------------------------------------------------- #
def test_read_events_filter_loop() -> bool:
    ok = True
    ing = _fresh()
    root = "read-filter"
    ing.ingest(root, sid="sid-A", event_type="agent_message",
               data=_agent("f1"), source="test", msg_id="m1")
    ing.ingest(root, sid="sid-B", event_type="agent_message",
               data=_agent("f2"), source="test", msg_id="m2")
    # sid_filter drops the non-matching row.
    out_a, total_a, _ = ing.read_events(root, sid_filter="sid-A")
    ok = _check([e["sid"] for e in out_a] == ["sid-A"] and total_a == 1,
                "sid_filter keeps only sid-A", f"{out_a}") and ok
    # msg_id_filter drops the non-matching row.
    out_m, total_m, _ = ing.read_events(root, msg_id_filter="m2")
    ok = _check([e.get("msg_id") for e in out_m] == ["m2"] and total_m == 1,
                "msg_id_filter keeps only m2", f"{out_m}") and ok
    # after_seq filter on the cached entries.
    out_s, total_s, _ = ing.read_events(root, after_seq=1)
    ok = _check(total_s == 1, "after_seq=1 filters out seq 1", f"{total_s}") and ok
    # limit below total -> has_more True, out capped.
    out_l, total_l, has_more_l = ing.read_events(root, limit=1)
    ok = _check(len(out_l) == 1 and total_l == 2 and has_more_l is True,
                "limit caps out, has_more True", f"{len(out_l)} {total_l} {has_more_l}") and ok
    return ok


# --------------------------------------------------------------------------- #
# _scan_from: blank-line skip + sid/msg_id filter branches (cold scan)
# --------------------------------------------------------------------------- #
def test_scan_from_blank_and_filters() -> bool:
    ok = True
    ing = _fresh()
    root = "scan-blank"
    # A blank line in the middle must be skipped without aborting the scan.
    _append_raw(root, {"seq": 1, "ts": "t", "sid": "sid-A", "type": "agent_message",
                       "data": _agent("b1"), "source": "test", "msg_id": "m1"})
    _append_raw_text(root, "   \n")
    _append_raw(root, {"seq": 2, "ts": "t", "sid": "sid-B", "type": "agent_message",
                       "data": _agent("b2"), "source": "test", "msg_id": "m2"})
    out, total, _ = ing.read_events(root, sid_filter="sid-B")
    ok = _check(total == 1 and out[0]["seq"] == 2,
                "blank line skipped, sid_filter keeps seq 2", f"{out}") and ok
    return ok


# --------------------------------------------------------------------------- #
# _scan_from: early-exit (populate_cache=False, len(matched) > limit)
# --------------------------------------------------------------------------- #
def test_scan_from_early_exit() -> bool:
    ok = True
    ing = _fresh()
    root, sid = "scan-early", "sid-A"
    for uid in ("e1", "e2", "e3"):
        ing.ingest(root, sid=sid, event_type="agent_message",
                   data=_agent(uid), source="test", msg_id="m1")
    # Fast path (offsets populated, after_seq>0) with a tiny limit triggers
    # the `not populate_cache and len(matched) > limit` early-exit break.
    out, total, has_more = ing.read_events(root, after_seq=1, limit=1)
    ok = _check(len(out) == 1 and has_more is True,
                "early-exit caps matched to limit, has_more True",
                f"{len(out)} {has_more}") and ok
    return ok


# --------------------------------------------------------------------------- #
# _scan_from: unlocked catch-up append. The catch-up branch fires when the
# file grows during the lock-released read. We drive the REAL _scan_from
# code deterministically (no threads, no sleeps): the on-disk file is the
# full file, but stat().st_size is staged so the snapshot captured at
# _scan_from entry sees only the base bytes, then the post-release stat
# sees the grown size -> the catch-up _consume re-reads the appended tail.
# --------------------------------------------------------------------------- #
class _StagedStatPath:
    """Duck-typed stand-in for the events.jsonl Path whose `.stat().st_size`
    is staged by call count. `_scan_from` / `read_events` only need
    `.stat()`, `.exists()`, and `.open()`. The first `base_calls` stat()
    calls report `base_size` (the snapshot captured before the simulated
    append); every later call reports the real on-disk size. This
    deterministically reproduces the race window the catch-up branch
    exists for, without threads or sleeps."""

    def __init__(self, target: str, base_size: int, base_calls: int):
        self._real = pathlib.Path(target)
        self._base_size = base_size
        self._base_calls = base_calls
        self._seen = 0

    def _staged_size(self) -> int:
        self._seen += 1
        if self._seen <= self._base_calls:
            return self._base_size
        return self._real.stat().st_size

    def stat(self):
        real = self._real.stat()
        return os.stat_result(
            (real.st_mode, real.st_ino, real.st_dev, real.st_nlink,
             real.st_uid, real.st_gid, self._staged_size(),
             real.st_atime, real.st_mtime, real.st_ctime)
        )

    def exists(self) -> bool:
        return self._real.exists()

    def open(self, *a, **k):
        return self._real.open(*a, **k)

    def __fspath__(self) -> str:
        return str(self._real)


def test_scan_from_unlocked_catchup() -> bool:
    ok = True
    ing = _fresh()
    root = "scan-catchup"
    # Base rows present at snapshot time.
    for i in range(3):
        _append_raw(root, {"seq": i + 1, "ts": "t", "sid": "sid-A",
                           "type": "agent_message", "data": _agent(f"b{i}"),
                           "source": "test", "msg_id": "m1"})
    real_path = _events_path(root)
    base_size = os.path.getsize(real_path)
    # Rows "appended during the released read".
    for i in range(3):
        _append_raw(root, {"seq": 100 + i, "ts": "t", "sid": "sid-A",
                           "type": "agent_message", "data": _agent(f"a{i}"),
                           "source": "catchup", "msg_id": "m1"})
    full_size = os.path.getsize(real_path)
    # Stage: the first 2 stat() calls (read_events cache-size at 1408 +
    # _scan_from snapshot at 1880) see base_size; the 3rd (_scan_from
    # catch-up check at 1891) sees full_size -> catch-up _consume fires.
    staged = _StagedStatPath(real_path, base_size=base_size, base_calls=2)
    ing._events_path = lambda r, _sp=staged: _sp  # type: ignore[assignment]

    out, total, _ = ing.read_events(root)
    ok = _check(full_size > base_size, "full file larger than base snapshot") and ok
    ok = _check(total == 6, "catch-up incorporated the 3 appended rows (6 total)",
                f"total={total}") and ok
    ok = _check([e["source"] for e in out].count("catchup") == 3,
                "appended rows came from the catch-up pass",
                f"{[e['source'] for e in out]}") and ok
    ok = _check(ing._next_offset.get(root) == full_size,
                "_next_offset advanced to full file size after catch-up",
                f"{ing._next_offset.get(root)} vs {full_size}") and ok
    return ok


# --------------------------------------------------------------------------- #
# read_ws_events: manager_event unwrap + else shape (mixed)
# --------------------------------------------------------------------------- #
def test_read_ws_events_unwrap() -> bool:
    ok = True
    ing = _fresh()
    root = "ws-events"
    ing.ingest(root, sid="sid-A", event_type="manager_event",
               data={"event": {"type": "worker_event", "x": 1}, "extra": 2},
               source="test", msg_id="m1")
    ing.ingest(root, sid="sid-A", event_type="agent_message",
               data=_agent("z1"), source="test", msg_id="m1")
    # Dict data + non-dict inner -> dropped (mirrors read_ws_events_range).
    ing.ingest(root, sid="sid-A", event_type="manager_event",
               data={"event": "not-a-dict"}, source="test", msg_id="m1")
    ws = ing.read_ws_events(root)
    # manager_event row unwrapped to its inner event dict.
    ok = _check(ws[0] == {"type": "worker_event", "x": 1},
                "manager_event unwrapped to inner event", f"{ws[0]}") and ok
    # non-manager row wrapped as {type, data}.
    ok = _check(ws[1].get("type") == "agent_message" and "data" in ws[1],
                "agent_message wrapped as {type, data}", f"{ws[1]}") and ok
    # The malformed-inner manager_event is dropped (only 2 rows survive).
    ok = _check(len(ws) == 2, "malformed-inner manager_event dropped",
                f"{len(ws)}") and ok
    return ok


# --------------------------------------------------------------------------- #
# read_ws_events_range: missing file
# --------------------------------------------------------------------------- #
def test_read_ws_events_range_missing_file() -> bool:
    ing = _fresh()
    return _check(ing.read_ws_events_range("nope", 0, 100) == [],
                  "missing file -> []")


# --------------------------------------------------------------------------- #
# read_ws_events_range: byte range unwrap + blank/malformed skip + EOF
# --------------------------------------------------------------------------- #
def test_read_ws_events_range_byte_range() -> bool:
    ok = True
    root = "ws-range"
    s1, e1 = _append_raw(root, {"seq": 1, "ts": "t", "sid": "sid-A",
                                "type": "manager_event",
                                "data": {"event": {"type": "worker_start"}},
                                "source": "test", "msg_id": "m1"})
    _append_raw_text(root, "\n")  # blank line skipped
    _append_raw_text(root, "{not json\n")  # malformed skipped
    s2, e2 = _append_raw(root, {"seq": 2, "ts": "t", "sid": "sid-A",
                                "type": "agent_message", "data": _agent("r1"),
                                "source": "test", "msg_id": "m1"})
    ing = _fresh()

    # Full range: manager unwrapped + agent wrapped; blank and malformed
    # lines skipped without aborting the scan; loop ends at EOF.
    ws = ing.read_ws_events_range(root, 0, e2 + 16)
    ok = _check(len(ws) == 2, "two valid rows decoded in range", f"{len(ws)}") and ok
    ok = _check(ws[0] == {"type": "worker_start"},
                "manager_event unwrapped in range", f"{ws[0]}") and ok
    ok = _check(ws[1].get("type") == "agent_message",
                "agent_message wrapped in range", f"{ws[1]}") and ok

    # Narrow range covering only the second row's bytes.
    narrow = ing.read_ws_events_range(root, s2, e2)
    ok = _check(len(narrow) == 1 and narrow[0].get("type") == "agent_message",
                "narrow byte range returns row 2 only", f"{narrow}") and ok

    # byte_end at the first row returns only row 1.
    only1 = ing.read_ws_events_range(root, 0, e1)
    ok = _check(len(only1) == 1 and only1[0] == {"type": "worker_start"},
                "range up to e1 returns row 1 only", f"{only1}") and ok

    # Range past EOF terminates cleanly (readline returns b"").
    past = ing.read_ws_events_range(root, e2 + 5, e2 + 10_000)
    ok = _check(past == [], "range past EOF -> []", f"{past}") and ok
    return ok


# --------------------------------------------------------------------------- #
# read_ws_events_range: manager_event with dict data + non-dict inner is
#                      DROPPED (defensive — only well-formed inner events
#                      are forwarded); manager_event with non-dict data
#                      falls through to the {type, data} else branch.
# --------------------------------------------------------------------------- #
def test_read_ws_events_range_manager_drop_and_else() -> bool:
    ok = True
    root = "ws-fall"
    # Dict data but non-dict inner -> dropped (not forwarded to the client).
    _append_raw(root, {"seq": 1, "ts": "t", "sid": "sid-A",
                       "type": "manager_event",
                       "data": {"event": "not-a-dict"}, "source": "test"})
    # Non-dict data -> outer else branch wraps as {type, data}.
    _append_raw(root, {"seq": 2, "ts": "t", "sid": "sid-A",
                       "type": "manager_event", "data": "scalar-data",
                       "source": "test"})
    ing = _fresh()
    ws = ing.read_ws_events_range(root, 0, 10_000)
    ok = _check(len(ws) == 1, "malformed-inner manager_event dropped, scalar kept",
                f"{len(ws)}") and ok
    ok = _check(ws[0].get("type") == "manager_event"
                and ws[0].get("data") == "scalar-data",
                "non-dict data falls to {type, data}", f"{ws[0]}") and ok
    return ok


TESTS = [
    test_read_events_missing_file,
    test_read_events_offset_fast_path,
    test_read_events_cold_scan_populate,
    test_read_events_cold_scan_trailing_malformed_skips_seed,
    test_read_events_cold_after_seq_filter,
    test_read_events_cache_hit_and_extend,
    test_read_events_filter_loop,
    test_scan_from_blank_and_filters,
    test_scan_from_early_exit,
    test_scan_from_unlocked_catchup,
    test_read_ws_events_unwrap,
    test_read_ws_events_range_missing_file,
    test_read_ws_events_range_byte_range,
    test_read_ws_events_range_manager_drop_and_else,
]


def main() -> int:
    results = []
    for test in TESTS:
        print(f"\n--- {test.__name__} ---")
        try:
            results.append(test())
        except Exception as exc:  # noqa: BLE001
            print(f"{FAIL} {test.__name__} raised: {exc!r}")
            results.append(False)
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} test groups passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
