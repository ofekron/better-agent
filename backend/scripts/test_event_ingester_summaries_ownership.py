"""Unit coverage for the summaries + ownership family of event_ingester.py:
message_event_summaries (+ _public_*/_summary_matches_filter), latest_render_event_uid,
worker_event_rows, ownership_resolutions[_range], and the machinery behind them
(_summaries_state, _seq_byte_range, _fold_resolutions, _update_summary_line,
_summary_render_event, _summary_preview_events, _append_summaries,
_rebuild_seq_offsets_locked, _scan_summaries).

Run with:
    cd backend && .venv/bin/python scripts/test_event_ingester_summaries_ownership.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ingester-summ-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_ingester import EventIngester  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


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


def _fresh() -> EventIngester:
    return EventIngester()


# --------------------------------------------------------------------------- #
# Missing-root read APIs
# --------------------------------------------------------------------------- #
def test_missing_root_reads() -> None:
    ing = _fresh()
    assert ing.message_event_summaries("nope") == {}, "summaries missing -> {}"
    assert ing.ownership_resolutions("nope") == {}, "resolutions missing -> {}"
    assert ing.worker_event_rows("nope", {"d1"}) == {}, "worker_rows missing -> {}"
    assert ing.latest_render_event_uid("nope") is None, "latest uid missing -> None"


# --------------------------------------------------------------------------- #
# message_event_summaries: basic shape + public stripping
# --------------------------------------------------------------------------- #
def test_message_event_summaries_basic() -> None:
    ing = _fresh()
    root, sid = "summ-basic", "sid-A"
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent("u1"), source="test", msg_id="m1")

    sums = ing.message_event_summaries(root)
    assert set(sums.keys()) == {"m1"}, f"summary keyed by msg_id -- {sorted(sums)}"
    rec = sums["m1"]
    assert rec.get("sid") == sid, "summary records sid"
    assert rec.get("event_count") == 1, f"event_count == 1 -- {rec.get('event_count')}"
    assert isinstance(rec.get("byte_start"), int) and isinstance(rec.get("byte_end"), int), \
        "byte bounds are ints"
    assert len(rec.get("last_events", [])) == 1, "one last_event"
    # Public API strips underscore-prefixed internal keys.
    assert not any(str(k).startswith("_") for k in rec), \
        f"public summary strips _ keys -- {sorted(rec)}"


# --------------------------------------------------------------------------- #
# Multiple events: append, same-uid replace, tail cap
# --------------------------------------------------------------------------- #
def test_summaries_append_replace_tail() -> None:
    ing = _fresh()
    root, sid = "summ-mult", "sid-A"
    for uid in ("a1", "a2", "a3"):
        ing.ingest(root, sid=sid, event_type="agent_message",
                   data=_agent(uid), source="test", msg_id="m1")
    rec = ing.message_event_summaries(root)["m1"]
    assert rec["event_count"] == 3, f"three distinct uids -> event_count 3 -- {rec['event_count']}"
    assert len(rec["last_events"]) == 3, "three last_events (tail default 25)"

    # Same-uid mutated event REPLACES the existing entry, does not increment count.
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent("a2", "changed"), source="test", msg_id="m1")
    rec = ing.message_event_summaries(root)["m1"]
    assert rec["event_count"] == 3, f"same-uid replace keeps event_count 3 -- {rec['event_count']}"

    # Tail cap: with tail=2 the preview keeps only the last 2 render events.
    ing2 = _fresh()
    root2 = "summ-tail"
    for uid in ("t1", "t2", "t3"):
        ing2.ingest(root2, sid=sid, event_type="agent_message",
                    data=_agent(uid), source="test", msg_id="m1")
    rec2 = ing2.message_event_summaries(root2, tail=2)["m1"]
    assert len(rec2["last_events"]) == 2, f"tail=2 caps last_events to 2 -- {len(rec2['last_events'])}"
    assert rec2["event_count"] == 3, f"event_count still counts all 3 under tail cap -- {rec2['event_count']}"


# --------------------------------------------------------------------------- #
# sid_filter / msg_ids filters
# --------------------------------------------------------------------------- #
def test_summaries_filters() -> None:
    ing = _fresh()
    root = "summ-filter"
    ing.ingest(root, sid="sid-A", event_type="agent_message",
               data=_agent("f1"), source="test", msg_id="m1")
    ing.ingest(root, sid="sid-B", event_type="agent_message",
               data=_agent("f2"), source="test", msg_id="m2")

    by_sid = ing.message_event_summaries(root, sid_filter="sid-A")
    assert set(by_sid.keys()) == {"m1"}, f"sid_filter keeps only sid-A msg -- {sorted(by_sid)}"
    by_ids = ing.message_event_summaries(root, msg_ids={"m2"})
    assert set(by_ids.keys()) == {"m2"}, f"msg_ids keeps only m2 -- {sorted(by_ids)}"
    both = ing.message_event_summaries(root, sid_filter="sid-A", msg_ids={"m2"})
    assert both == {}, f"sid+msg_ids mismatch -> {{}} -- {sorted(both)}"


def test_summary_helpers_direct() -> None:
    pub = EventIngester._public_message_summary({"a": 1, "_b": 2, "sid": "s"})
    assert pub == {"a": 1, "sid": "s"}, f"_public_message_summary strips _ keys -- {pub}"
    pubs = EventIngester._public_message_summaries({"m": {"x": 1, "_y": 2}})
    assert pubs == {"m": {"x": 1}}, f"_public_message_summaries maps+strips -- {pubs}"

    matches = EventIngester._summary_matches_filter
    assert matches("m1", {"sid": "s"}, sid_filter="s", msg_ids=None) is True, \
        "filter: sid match, no msg_ids"
    assert matches("m1", {"sid": "s"}, sid_filter="other", msg_ids=None) is False, \
        "filter: sid mismatch"
    assert matches("m1", {"sid": "s"}, sid_filter=None, msg_ids={"m1"}) is True, \
        "filter: msg_id in set"
    assert matches("m1", {"sid": "s"}, sid_filter=None, msg_ids={"m9"}) is False, \
        "filter: msg_id not in set"


# --------------------------------------------------------------------------- #
# ownership_resolutions / ownership_resolutions_range
# --------------------------------------------------------------------------- #
def test_ownership_resolutions() -> None:
    ing = _fresh()
    root, sid = "own-res", "sid-A"
    # An orphan at seq 1, then a resolution pointing seq 1 -> msg "m-resolved".
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent("o1"), source="test")                  # seq 1 (orphan)
    ing.ingest(root, sid=sid, event_type="event_ownership_resolved",
               data={"event_seq": 1, "message_id": "m-resolved"}, source="test")  # seq 2

    res = ing.ownership_resolutions(root)
    assert res == {1: "m-resolved"}, f"resolution map {{seq: msg_id}} -- {res}"

    rng = ing.ownership_resolutions_range(root, seq_start=1, seq_end=5)
    assert rng == {1: "m-resolved"}, f"range filter includes seq -- {rng}"
    rng_none = ing.ownership_resolutions_range(root, seq_start=5, seq_end=10)
    assert rng_none == {}, f"range filter excludes seq -> {{}} -- {rng_none}"
    rng_bad = ing.ownership_resolutions_range(root, seq_start=5, seq_end=1)
    assert rng_bad == {}, f"seq_end<seq_start -> {{}} -- {rng_bad}"

    # Malformed ownership rows (non-int seq / empty target) are skipped.
    ing2 = _fresh()
    root2 = "own-malformed"
    _append_raw(root2, {"seq": 1, "ts": "t", "sid": sid, "type": "event_ownership_resolved",
                        "data": {"event_seq": "nope", "message_id": "m"}, "source": "x"})
    _append_raw(root2, {"seq": 2, "ts": "t", "sid": sid, "type": "event_ownership_resolved",
                        "data": {"event_seq": 5, "message_id": ""}, "source": "x"})
    assert ing2.ownership_resolutions(root2) == {}, "malformed ownership rows skipped"


# --------------------------------------------------------------------------- #
# worker_event_rows
# --------------------------------------------------------------------------- #
def _worker_event(delegation_id: str, sid: str) -> dict:
    return {
        "uuid": f"w-{delegation_id}", "type": "worker_event",
        "event": {"type": "worker_event", "data": {"uuid": f"w-{delegation_id}"}},
        "delegation_id": delegation_id,
        "sid": sid,
    }


def test_worker_event_rows() -> None:
    ing = _fresh()
    root, sid = "worker-rows", "sid-A"
    ing.ingest(root, sid=sid, event_type="worker_event",
               data=_worker_event("del-1", sid), source="test")
    ing.ingest(root, sid=sid, event_type="worker_event",
               data=_worker_event("del-2", sid), source="test")

    rows = ing.worker_event_rows(root, {"del-1", "del-2"})
    assert set(rows.keys()) == {"del-1", "del-2"}, f"rows for both delegations -- {sorted(rows)}"
    assert rows["del-1"][0]["type"] == "worker_event", "row parsed as dict"

    # sid_filter narrows; here rows share the same sid so still present.
    rows_sid = ing.worker_event_rows(root, {"del-1"}, sid_filter=sid)
    assert set(rows_sid.keys()) == {"del-1"}, "sid_filter keeps matching delegation"
    # sid_filter that matches nothing drops the delegation.
    rows_other = ing.worker_event_rows(root, {"del-1"}, sid_filter="other-sid")
    assert rows_other == {}, "sid_filter mismatch drops delegation"

    # Empty delegation_ids / unknown delegation -> {}.
    assert ing.worker_event_rows(root, set()) == {}, "empty delegation_ids -> {}"
    assert ing.worker_event_rows(root, {"unknown"}) == {}, "unknown delegation -> {}"
    assert ing.worker_event_rows("missing", {"del-1"}) == {}, "missing file -> {}"


def test_worker_event_rows_malformed() -> None:
    """A span whose first line is not valid JSON is skipped on read."""
    ing = _fresh()
    root, sid = "worker-mal", "sid-A"
    ing.ingest(root, sid=sid, event_type="worker_event",
               data=_worker_event("del-x", sid), source="test")
    # Drive a summaries scan so the delegation span index is populated and the
    # summaries cache is warm (so the second read below does not rescan).
    assert ing.worker_event_rows(root, {"del-x"}) != {}, "baseline worker row present"
    spans = ing._worker_rows[root]["del-x"]
    first_start = spans[0][0]
    span_len = spans[0][1] - first_start

    # Corrupt the on-disk bytes the warm span points at; the next read parses
    # invalid JSON and skips the row.
    path = _events_path(root)
    with open(path, "r+b") as fh:
        fh.seek(first_start)
        fh.write(b"x" * span_len)
    rows = ing.worker_event_rows(root, {"del-x"})
    assert rows == {}, f"unparseable worker span -> skipped -- {rows}"


# --------------------------------------------------------------------------- #
# latest_render_event_uid
# --------------------------------------------------------------------------- #
def test_latest_render_event_uid() -> None:
    ing = _fresh()
    root, sid = "latest-uid", "sid-A"
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent("late1"), source="test", msg_id="m1")
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent("late2"), source="test", msg_id="m1")
    uid = ing.latest_render_event_uid(root)
    assert uid == "late2", f"latest render uid is the last appended -- {uid}"

    # sid_filter path: hits the per-sid cache populated by the scan above.
    uid_sid = ing.latest_render_event_uid(root, sid_filter=sid)
    assert uid_sid == "late2", f"sid_filter returns latest uid -- {uid_sid}"
    uid_other = ing.latest_render_event_uid(root, sid_filter="no-such-sid")
    assert uid_other is None, f"sid_filter with no events -> None -- {uid_other}"


# --------------------------------------------------------------------------- #
# _seq_byte_range edge cases (direct)
# --------------------------------------------------------------------------- #
def test_seq_byte_range_edges() -> None:
    ing = _fresh()
    root = "byterange-direct"
    ing._seq_offsets[root] = [10, 20, 30]
    ing._next_offset[root] = 45
    assert ing._seq_byte_range(root, 1) == (10, 20), "seq 1 -> (10,20)"
    assert ing._seq_byte_range(root, 2) == (20, 30), "seq 2 -> (20,30)"
    assert ing._seq_byte_range(root, 3) == (30, 45), "last seq uses _next_offset"
    assert ing._seq_byte_range(root, 0) is None, "seq<1 -> None"
    assert ing._seq_byte_range(root, 4) is None, "seq>len -> None"
    assert ing._seq_byte_range("no-offsets", 1) is None, "no offsets -> None"
    # end<=start guard: make the last offset equal next_offset so seq 3 end==start.
    ing._seq_offsets[root] = [10, 45]
    ing._next_offset[root] = 45
    assert ing._seq_byte_range(root, 2) is None, "end<=start -> None"


# --------------------------------------------------------------------------- #
# _fold_resolutions: bound expansion + missing-rec creation (direct)
# --------------------------------------------------------------------------- #
def test_fold_resolutions() -> None:
    ing = _fresh()
    root = "fold-direct"
    ing._seq_offsets[root] = [100, 200, 300]
    ing._next_offset[root] = 360
    summaries = {"m1": {"msg_id": "m1", "seq_start": 2, "seq_end": 2,
                        "byte_start": 200, "byte_end": 300, "event_count": 1,
                        "last_events": []}}
    # Resolution: orphan seq 1 (bytes 100-200) folds into m1 -> bounds expand.
    ing._fold_resolutions(root, summaries, {1: "m1"})
    rec = summaries["m1"]
    assert rec["byte_start"] == 100 and rec["byte_end"] == 300, \
        f"fold expands byte bounds to cover orphan -- {rec['byte_start']},{rec['byte_end']}"
    assert rec["seq_start"] == 1 and rec["seq_end"] == 2, \
        f"fold expands seq bounds -- {rec['seq_start']},{rec['seq_end']}"

    # Resolution whose msg_id has no existing record creates a fresh stub rec.
    ing._fold_resolutions(root, summaries, {3: "m-new"})
    assert "m-new" in summaries, f"fold creates missing rec -- {sorted(summaries)}"
    new = summaries["m-new"]
    assert new["seq_start"] == 3 and new["byte_start"] == 300, "created rec seeded from orphan range"

    # Resolution for a seq with no byte range is skipped (None rng).
    before = dict(summaries)
    ing._fold_resolutions(root, summaries, {99: "m1"})
    assert summaries == before, "resolution with no byte range is a no-op"


# --------------------------------------------------------------------------- #
# _update_summary_line branches (direct)
# --------------------------------------------------------------------------- #
def test_update_summary_line_branches() -> None:
    ing = _fresh()
    root = "upd-direct"
    out: dict[str, dict] = {}
    resolutions: dict[int, str] = {}

    # worker_event records a delegation span in _worker_rows (no summary rec).
    ing._update_summary_line(out, resolutions, root,
                             {"type": "worker_event", "seq": 1, "sid": "s",
                              "data": {"delegation_id": "del-z"}}, 10, 20, 25)
    assert ing._worker_rows[root].get("del-z") == [(10, 20)], "worker_event records delegation span"
    assert out == {}, "worker_event creates no summary rec"

    # event_ownership_resolved records a resolution and returns.
    ing._update_summary_line(out, resolutions, root,
                             {"type": "event_ownership_resolved", "seq": 2, "sid": "s",
                              "data": {"event_seq": 7, "message_id": "m-r"}}, 30, 40, 25)
    assert resolutions == {7: "m-r"}, "ownership row records resolution"
    assert out == {}, "ownership row creates no summary rec"

    # Row without a msg_id is dropped.
    ing._update_summary_line(out, resolutions, root,
                             {"type": "agent_message", "seq": 3, "sid": "s",
                              "data": _agent("x")}, 50, 60, 25)
    assert out == {}, "row with no msg_id dropped"

    # worker_start / worker_complete bump worker_panel_event_count (no render event).
    ing._update_summary_line(out, resolutions, root,
                             {"type": "worker_start", "seq": 4, "sid": "s",
                              "msg_id": "m1", "data": {"delegation_id": "d"}},
                             70, 80, 25)
    ing._update_summary_line(out, resolutions, root,
                             {"type": "worker_complete", "seq": 5, "sid": "s",
                              "msg_id": "m1", "data": {"delegation_id": "d"}},
                             90, 100, 25)
    assert out["m1"]["worker_panel_event_count"] == 2, \
        f"worker_start/complete counted -- {out['m1']['worker_panel_event_count']}"
    assert out["m1"]["event_count"] == 0, "worker_start/complete adds no render event"

    # agent_message render event appends + counts.
    ing._update_summary_line(out, resolutions, root,
                             {"type": "agent_message", "seq": 6, "sid": "s",
                              "msg_id": "m1", "data": _agent("r1")}, 110, 120, 25)
    assert out["m1"]["event_count"] == 1 and len(out["m1"]["last_events"]) == 1, \
        "agent_message render event counted + appended"

    # Same-uid agent_message replaces the existing last_event (no count bump).
    ing._update_summary_line(out, resolutions, root,
                             {"type": "agent_message", "seq": 7, "sid": "s",
                              "msg_id": "m1", "data": _agent("r1", "v2")}, 130, 140, 25)
    assert out["m1"]["event_count"] == 1, "same-uid replace keeps count"
    assert len(out["m1"]["last_events"]) == 1, "same-uid replace keeps list len"

    # agent_message whose data has NO uuid is skipped (no count, no append).
    ing._update_summary_line(out, resolutions, root,
                             {"type": "agent_message", "seq": 8, "sid": "s",
                              "msg_id": "m1",
                              "data": {"type": "assistant", "message": {"content": []}}},
                             150, 160, 25)
    assert out["m1"]["event_count"] == 1, "no-uid render event skipped"

    # Non-int seq: rec still created + render event counted, but seq bounds
    # are not updated past the setdefault value.
    ing._update_summary_line(out, resolutions, root,
                             {"type": "agent_message", "seq": "notint", "sid": "s",
                              "msg_id": "m-noint", "data": _agent("ni1")}, 170, 180, 25)
    assert out.get("m-noint", {}).get("event_count") == 1, \
        f"non-int seq still counts render event -- {out.get('m-noint')}"

    # Replace-loop no-match: uuid is in _render_uuid_idx but was tail-evicted
    # out of last_events -> the loop finds no match and returns without change.
    out["m-evict"] = {"msg_id": "m-evict", "sid": "s", "seq_start": 1, "seq_end": 1,
                      "byte_start": 0, "byte_end": 10, "event_count": 1,
                      "worker_panel_event_count": 0,
                      "last_events": [{"type": "agent_message", "data": {"uuid": "kept"}}],
                      "_render_uuid_idx": {"evicted": 0}}
    ing._update_summary_line(out, resolutions, root,
                             {"type": "agent_message", "seq": 2, "sid": "s",
                              "msg_id": "m-evict", "data": _agent("evicted")}, 190, 200, 25)
    assert out["m-evict"]["event_count"] == 1 and len(out["m-evict"]["last_events"]) == 1, \
        "evicted-uid replace leaves count+list unchanged"


# --------------------------------------------------------------------------- #
# Incremental summaries append (warm cache -> _append_summaries path)
# --------------------------------------------------------------------------- #
def test_incremental_summaries_append() -> None:
    ing = _fresh()
    root, sid = "summ-incr", "sid-A"
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent("i1"), source="test", msg_id="m1")
    ing.message_event_summaries(root)                              # warm cache
    cached_size = ing._summaries_cache[root][0]

    # Append a blank + malformed line directly; the incremental scan must
    # skip both when catching up.
    with open(_events_path(root), "a", encoding="utf-8") as fh:
        fh.write("\n{ not valid json\n")
    # Append more through ingest; the next read takes the incremental branch.
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent("i2"), source="test", msg_id="m1")
    new_size = ing._summaries_cache[root][0]
    # _summaries_state refreshes the cache size to the new file size on read.
    rec = ing.message_event_summaries(root)["m1"]
    assert rec["event_count"] == 2, f"incremental append picked up new event past junk -- {rec['event_count']}"
    assert ing._summaries_cache[root][0] >= new_size, \
        f"cache high-water advanced -- {ing._summaries_cache[root][0]}"


# --------------------------------------------------------------------------- #
# Cold scan + _rebuild_seq_offsets_locked (no sidecar, fresh ingester)
# --------------------------------------------------------------------------- #
def test_cold_scan_summaries_and_rebuild_offsets() -> None:
    root, sid = "summ-cold", "sid-A"
    _append_raw(root, {"seq": 1, "ts": "t", "sid": sid, "type": "agent_message",
                       "data": _agent("c1"), "source": "x", "msg_id": "m1"})
    # Blank + malformed lines that the cold scan must skip.
    with open(_events_path(root), "a", encoding="utf-8") as fh:
        fh.write("\n{ broken json\n")
    _append_raw(root, {"seq": 2, "ts": "t", "sid": sid, "type": "agent_message",
                       "data": _agent("c2"), "source": "x", "msg_id": "m1"})

    ing = _fresh()                                                 # no in-memory state
    rec = ing.message_event_summaries(root).get("m1", {})
    assert rec.get("event_count") == 2, f"cold full scan built summaries -- {rec.get('event_count')}"
    assert ing._seq_offsets.get(root) is not None \
        and len(ing._seq_offsets[root]) == 2, "cold scan populated _seq_offsets"

    # _rebuild_seq_offsets_locked re-derives offsets from disk and resets _seq.
    ing._seq_offsets[root] = [999]                                  # corrupt
    ing._rebuild_seq_offsets_locked(pathlib.Path(_events_path(root)), root)
    assert len(ing._seq_offsets[root]) == 2, \
        f"rebuild_offsets restored 2 offsets -- {len(ing._seq_offsets[root])}"
    assert ing._seq[root] == 2, "rebuild_offsets reset _seq count"


# --------------------------------------------------------------------------- #
# _scan_summaries full-scan-cache fast path (full_scan_cache matches file)
# --------------------------------------------------------------------------- #
def test_scan_summaries_uses_full_scan_cache() -> None:
    """When _full_scan_cache matches the file size and offsets are aligned,
    _scan_summaries reuses parsed entries instead of re-reading the file."""
    ing = _fresh()
    root, sid = "summ-fscache", "sid-A"
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent("fc1"), source="test", msg_id="m1")
    path = pathlib.Path(_events_path(root))

    # Seed _full_scan_cache + _seq_offsets to satisfy the cache-fast-path
    # condition (cached[0]==file_size and len(offsets)==len(cached[1])).
    # The fast path must NOT reassign _seq_offsets, so we keep a reference.
    entries: list[dict] = []
    offsets: list[int] = []
    with open(path, "rb") as fh:
        while True:
            start = fh.tell()
            raw = fh.readline()
            if not raw:
                break
            offsets.append(start)
            entries.append(json.loads(raw.decode("utf-8")))
    file_size = path.stat().st_size
    ing._full_scan_cache[root] = (file_size, entries)
    ing._seq_offsets[root] = offsets
    ing._next_offset[root] = file_size
    offsets_identity = id(ing._seq_offsets[root])

    out, _res = ing._scan_summaries(path, root, 25)
    assert out.get("m1", {}).get("event_count") == 1, \
        f"scan_summaries built summary from cached entries -- {out}"
    assert id(ing._seq_offsets[root]) == offsets_identity, \
        "fast path reused _seq_offsets without reassigning"


TESTS = [
    test_missing_root_reads,
    test_message_event_summaries_basic,
    test_summaries_append_replace_tail,
    test_summaries_filters,
    test_summary_helpers_direct,
    test_ownership_resolutions,
    test_worker_event_rows,
    test_worker_event_rows_malformed,
    test_latest_render_event_uid,
    test_seq_byte_range_edges,
    test_fold_resolutions,
    test_update_summary_line_branches,
    test_incremental_summaries_append,
    test_cold_scan_summaries_and_rebuild_offsets,
    test_scan_summaries_uses_full_scan_cache,
]


def main() -> None:
    failed = 0
    for test in TESTS:
        print(f"\n--- {test.__name__} ---")
        try:
            test()
        except AssertionError as exc:
            print(f"{FAIL} {test.__name__}: {exc}")
            failed += 1
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            failed += 1
        else:
            print(f"{PASS} {test.__name__}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} test groups passed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
