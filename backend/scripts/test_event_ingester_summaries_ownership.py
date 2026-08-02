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


def _fresh() -> EventIngester:
    return EventIngester()


# --------------------------------------------------------------------------- #
# Missing-root read APIs
# --------------------------------------------------------------------------- #
def test_missing_root_reads() -> bool:
    ok = True
    ing = _fresh()
    ok = _check(ing.message_event_summaries("nope") == {}, "summaries missing -> {}") and ok
    ok = _check(ing.ownership_resolutions("nope") == {}, "resolutions missing -> {}") and ok
    ok = _check(ing.worker_event_rows("nope", {"d1"}) == {}, "worker_rows missing -> {}") and ok
    ok = _check(ing.latest_render_event_uid("nope") is None, "latest uid missing -> None") and ok
    return ok


# --------------------------------------------------------------------------- #
# message_event_summaries: basic shape + public stripping
# --------------------------------------------------------------------------- #
def test_message_event_summaries_basic() -> bool:
    ok = True
    ing = _fresh()
    root, sid = "summ-basic", "sid-A"
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent("u1"), source="test", msg_id="m1")

    sums = ing.message_event_summaries(root)
    ok = _check(set(sums.keys()) == {"m1"}, "summary keyed by msg_id", f"{sorted(sums)}") and ok
    rec = sums["m1"]
    ok = _check(rec.get("sid") == sid, "summary records sid") and ok
    ok = _check(rec.get("event_count") == 1, "event_count == 1", f"{rec.get('event_count')}") and ok
    ok = _check(isinstance(rec.get("byte_start"), int) and isinstance(rec.get("byte_end"), int),
                "byte bounds are ints") and ok
    ok = _check(len(rec.get("last_events", [])) == 1, "one last_event") and ok
    # Public API strips underscore-prefixed internal keys.
    ok = _check(not any(str(k).startswith("_") for k in rec),
                "public summary strips _ keys", f"{sorted(rec)}") and ok
    return ok


# --------------------------------------------------------------------------- #
# Multiple events: append, same-uid replace, tail cap
# --------------------------------------------------------------------------- #
def test_summaries_append_replace_tail() -> bool:
    ok = True
    ing = _fresh()
    root, sid = "summ-mult", "sid-A"
    for uid in ("a1", "a2", "a3"):
        ing.ingest(root, sid=sid, event_type="agent_message",
                   data=_agent(uid), source="test", msg_id="m1")
    rec = ing.message_event_summaries(root)["m1"]
    ok = _check(rec["event_count"] == 3, "three distinct uids -> event_count 3",
                f"{rec['event_count']}") and ok
    ok = _check(len(rec["last_events"]) == 3, "three last_events (tail default 25)") and ok

    # Same-uid mutated event REPLACES the existing entry, does not increment count.
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent("a2", "changed"), source="test", msg_id="m1")
    rec = ing.message_event_summaries(root)["m1"]
    ok = _check(rec["event_count"] == 3, "same-uid replace keeps event_count 3",
                f"{rec['event_count']}") and ok

    # Tail cap: with tail=2 the preview keeps only the last 2 render events.
    ing2 = _fresh()
    root2 = "summ-tail"
    for uid in ("t1", "t2", "t3"):
        ing2.ingest(root2, sid=sid, event_type="agent_message",
                    data=_agent(uid), source="test", msg_id="m1")
    rec2 = ing2.message_event_summaries(root2, tail=2)["m1"]
    ok = _check(len(rec2["last_events"]) == 2, "tail=2 caps last_events to 2",
                f"{len(rec2['last_events'])}") and ok
    ok = _check(rec2["event_count"] == 3, "event_count still counts all 3 under tail cap",
                f"{rec2['event_count']}") and ok
    return ok


# --------------------------------------------------------------------------- #
# sid_filter / msg_ids filters
# --------------------------------------------------------------------------- #
def test_summaries_filters() -> bool:
    ok = True
    ing = _fresh()
    root = "summ-filter"
    ing.ingest(root, sid="sid-A", event_type="agent_message",
               data=_agent("f1"), source="test", msg_id="m1")
    ing.ingest(root, sid="sid-B", event_type="agent_message",
               data=_agent("f2"), source="test", msg_id="m2")

    by_sid = ing.message_event_summaries(root, sid_filter="sid-A")
    ok = _check(set(by_sid.keys()) == {"m1"}, "sid_filter keeps only sid-A msg",
                f"{sorted(by_sid)}") and ok
    by_ids = ing.message_event_summaries(root, msg_ids={"m2"})
    ok = _check(set(by_ids.keys()) == {"m2"}, "msg_ids keeps only m2", f"{sorted(by_ids)}") and ok
    both = ing.message_event_summaries(root, sid_filter="sid-A", msg_ids={"m2"})
    ok = _check(both == {}, "sid+msg_ids mismatch -> {}", f"{sorted(both)}") and ok
    return ok


def test_summary_helpers_direct() -> bool:
    ok = True
    pub = EventIngester._public_message_summary({"a": 1, "_b": 2, "sid": "s"})
    ok = _check(pub == {"a": 1, "sid": "s"}, "_public_message_summary strips _ keys",
                f"{pub}") and ok
    pubs = EventIngester._public_message_summaries({"m": {"x": 1, "_y": 2}})
    ok = _check(pubs == {"m": {"x": 1}}, "_public_message_summaries maps+strips") and ok

    matches = EventIngester._summary_matches_filter
    ok = _check(matches("m1", {"sid": "s"}, sid_filter="s", msg_ids=None) is True,
                "filter: sid match, no msg_ids") and ok
    ok = _check(matches("m1", {"sid": "s"}, sid_filter="other", msg_ids=None) is False,
                "filter: sid mismatch") and ok
    ok = _check(matches("m1", {"sid": "s"}, sid_filter=None, msg_ids={"m1"}) is True,
                "filter: msg_id in set") and ok
    ok = _check(matches("m1", {"sid": "s"}, sid_filter=None, msg_ids={"m9"}) is False,
                "filter: msg_id not in set") and ok
    return ok


# --------------------------------------------------------------------------- #
# ownership_resolutions / ownership_resolutions_range
# --------------------------------------------------------------------------- #
def test_ownership_resolutions() -> bool:
    ok = True
    ing = _fresh()
    root, sid = "own-res", "sid-A"
    # An orphan at seq 1, then a resolution pointing seq 1 -> msg "m-resolved".
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent("o1"), source="test")                  # seq 1 (orphan)
    ing.ingest(root, sid=sid, event_type="event_ownership_resolved",
               data={"event_seq": 1, "message_id": "m-resolved"}, source="test")  # seq 2

    res = ing.ownership_resolutions(root)
    ok = _check(res == {1: "m-resolved"}, "resolution map {seq: msg_id}", f"{res}") and ok

    rng = ing.ownership_resolutions_range(root, seq_start=1, seq_end=5)
    ok = _check(rng == {1: "m-resolved"}, "range filter includes seq", f"{rng}") and ok
    rng_none = ing.ownership_resolutions_range(root, seq_start=5, seq_end=10)
    ok = _check(rng_none == {}, "range filter excludes seq -> {}", f"{rng_none}") and ok
    rng_bad = ing.ownership_resolutions_range(root, seq_start=5, seq_end=1)
    ok = _check(rng_bad == {}, "seq_end<seq_start -> {}", f"{rng_bad}") and ok

    # Malformed ownership rows (non-int seq / empty target) are skipped.
    ing2 = _fresh()
    root2 = "own-malformed"
    _append_raw(root2, {"seq": 1, "ts": "t", "sid": sid, "type": "event_ownership_resolved",
                        "data": {"event_seq": "nope", "message_id": "m"}, "source": "x"})
    _append_raw(root2, {"seq": 2, "ts": "t", "sid": sid, "type": "event_ownership_resolved",
                        "data": {"event_seq": 5, "message_id": ""}, "source": "x"})
    ok = _check(ing2.ownership_resolutions(root2) == {}, "malformed ownership rows skipped") and ok
    return ok


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


def test_worker_event_rows() -> bool:
    ok = True
    ing = _fresh()
    root, sid = "worker-rows", "sid-A"
    ing.ingest(root, sid=sid, event_type="worker_event",
               data=_worker_event("del-1", sid), source="test")
    ing.ingest(root, sid=sid, event_type="worker_event",
               data=_worker_event("del-2", sid), source="test")

    rows = ing.worker_event_rows(root, {"del-1", "del-2"})
    ok = _check(set(rows.keys()) == {"del-1", "del-2"}, "rows for both delegations",
                f"{sorted(rows)}") and ok
    ok = _check(rows["del-1"][0]["type"] == "worker_event", "row parsed as dict") and ok

    # sid_filter narrows; here rows share the same sid so still present.
    rows_sid = ing.worker_event_rows(root, {"del-1"}, sid_filter=sid)
    ok = _check(set(rows_sid.keys()) == {"del-1"}, "sid_filter keeps matching delegation") and ok
    # sid_filter that matches nothing drops the delegation.
    rows_other = ing.worker_event_rows(root, {"del-1"}, sid_filter="other-sid")
    ok = _check(rows_other == {}, "sid_filter mismatch drops delegation") and ok

    # Empty delegation_ids / unknown delegation -> {}.
    ok = _check(ing.worker_event_rows(root, set()) == {}, "empty delegation_ids -> {}") and ok
    ok = _check(ing.worker_event_rows(root, {"unknown"}) == {}, "unknown delegation -> {}") and ok
    ok = _check(ing.worker_event_rows("missing", {"del-1"}) == {}, "missing file -> {}") and ok
    return ok


def test_worker_event_rows_malformed() -> bool:
    """A span whose first line is not valid JSON is skipped on read."""
    ok = True
    ing = _fresh()
    root, sid = "worker-mal", "sid-A"
    ing.ingest(root, sid=sid, event_type="worker_event",
               data=_worker_event("del-x", sid), source="test")
    # Drive a summaries scan so the delegation span index is populated and the
    # summaries cache is warm (so the second read below does not rescan).
    ok = _check(ing.worker_event_rows(root, {"del-x"}) != {}, "baseline worker row present") and ok
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
    ok = _check(rows == {}, "unparseable worker span -> skipped", f"{rows}") and ok
    return ok


# --------------------------------------------------------------------------- #
# latest_render_event_uid
# --------------------------------------------------------------------------- #
def test_latest_render_event_uid() -> bool:
    ok = True
    ing = _fresh()
    root, sid = "latest-uid", "sid-A"
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent("late1"), source="test", msg_id="m1")
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent("late2"), source="test", msg_id="m1")
    uid = ing.latest_render_event_uid(root)
    ok = _check(uid == "late2", "latest render uid is the last appended", f"{uid}") and ok

    # sid_filter path: hits the per-sid cache populated by the scan above.
    uid_sid = ing.latest_render_event_uid(root, sid_filter=sid)
    ok = _check(uid_sid == "late2", "sid_filter returns latest uid", f"{uid_sid}") and ok
    uid_other = ing.latest_render_event_uid(root, sid_filter="no-such-sid")
    ok = _check(uid_other is None, "sid_filter with no events -> None", f"{uid_other}") and ok
    return ok


# --------------------------------------------------------------------------- #
# _seq_byte_range edge cases (direct)
# --------------------------------------------------------------------------- #
def test_seq_byte_range_edges() -> bool:
    ok = True
    ing = _fresh()
    root = "byterange-direct"
    ing._seq_offsets[root] = [10, 20, 30]
    ing._next_offset[root] = 45
    ok = _check(ing._seq_byte_range(root, 1) == (10, 20), "seq 1 -> (10,20)") and ok
    ok = _check(ing._seq_byte_range(root, 2) == (20, 30), "seq 2 -> (20,30)") and ok
    ok = _check(ing._seq_byte_range(root, 3) == (30, 45), "last seq uses _next_offset") and ok
    ok = _check(ing._seq_byte_range(root, 0) is None, "seq<1 -> None") and ok
    ok = _check(ing._seq_byte_range(root, 4) is None, "seq>len -> None") and ok
    ok = _check(ing._seq_byte_range("no-offsets", 1) is None, "no offsets -> None") and ok
    # end<=start guard: make the last offset equal next_offset so seq 3 end==start.
    ing._seq_offsets[root] = [10, 45]
    ing._next_offset[root] = 45
    ok = _check(ing._seq_byte_range(root, 2) is None, "end<=start -> None") and ok
    return ok


# --------------------------------------------------------------------------- #
# _fold_resolutions: bound expansion + missing-rec creation (direct)
# --------------------------------------------------------------------------- #
def test_fold_resolutions() -> bool:
    ok = True
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
    ok = _check(rec["byte_start"] == 100 and rec["byte_end"] == 300,
                "fold expands byte bounds to cover orphan", f"{rec['byte_start']},{rec['byte_end']}") and ok
    ok = _check(rec["seq_start"] == 1 and rec["seq_end"] == 2,
                "fold expands seq bounds", f"{rec['seq_start']},{rec['seq_end']}") and ok

    # Resolution whose msg_id has no existing record creates a fresh stub rec.
    ing._fold_resolutions(root, summaries, {3: "m-new"})
    ok = _check("m-new" in summaries, "fold creates missing rec", f"{sorted(summaries)}") and ok
    new = summaries["m-new"]
    ok = _check(new["seq_start"] == 3 and new["byte_start"] == 300,
                "created rec seeded from orphan range") and ok

    # Resolution for a seq with no byte range is skipped (None rng).
    before = dict(summaries)
    ing._fold_resolutions(root, summaries, {99: "m1"})
    ok = _check(summaries == before, "resolution with no byte range is a no-op") and ok
    return ok


# --------------------------------------------------------------------------- #
# _update_summary_line branches (direct)
# --------------------------------------------------------------------------- #
def test_update_summary_line_branches() -> bool:
    ok = True
    ing = _fresh()
    root = "upd-direct"
    out: dict[str, dict] = {}
    resolutions: dict[int, str] = {}

    # worker_event records a delegation span in _worker_rows (no summary rec).
    ing._update_summary_line(out, resolutions, root,
                             {"type": "worker_event", "seq": 1, "sid": "s",
                              "data": {"delegation_id": "del-z"}}, 10, 20, 25)
    ok = _check(ing._worker_rows[root].get("del-z") == [(10, 20)],
                "worker_event records delegation span") and ok
    ok = _check(out == {}, "worker_event creates no summary rec") and ok

    # event_ownership_resolved records a resolution and returns.
    ing._update_summary_line(out, resolutions, root,
                             {"type": "event_ownership_resolved", "seq": 2, "sid": "s",
                              "data": {"event_seq": 7, "message_id": "m-r"}}, 30, 40, 25)
    ok = _check(resolutions == {7: "m-r"}, "ownership row records resolution") and ok
    ok = _check(out == {}, "ownership row creates no summary rec") and ok

    # Row without a msg_id is dropped.
    ing._update_summary_line(out, resolutions, root,
                             {"type": "agent_message", "seq": 3, "sid": "s",
                              "data": _agent("x")}, 50, 60, 25)
    ok = _check(out == {}, "row with no msg_id dropped") and ok

    # worker_start / worker_complete bump worker_panel_event_count (no render event).
    ing._update_summary_line(out, resolutions, root,
                             {"type": "worker_start", "seq": 4, "sid": "s",
                              "msg_id": "m1", "data": {"delegation_id": "d"}},
                             70, 80, 25)
    ing._update_summary_line(out, resolutions, root,
                             {"type": "worker_complete", "seq": 5, "sid": "s",
                              "msg_id": "m1", "data": {"delegation_id": "d"}},
                             90, 100, 25)
    ok = _check(out["m1"]["worker_panel_event_count"] == 2,
                "worker_start/complete counted", f"{out['m1']['worker_panel_event_count']}") and ok
    ok = _check(out["m1"]["event_count"] == 0, "worker_start/complete adds no render event") and ok

    # agent_message render event appends + counts.
    ing._update_summary_line(out, resolutions, root,
                             {"type": "agent_message", "seq": 6, "sid": "s",
                              "msg_id": "m1", "data": _agent("r1")}, 110, 120, 25)
    ok = _check(out["m1"]["event_count"] == 1 and len(out["m1"]["last_events"]) == 1,
                "agent_message render event counted + appended") and ok

    # Same-uid agent_message replaces the existing last_event (no count bump).
    ing._update_summary_line(out, resolutions, root,
                             {"type": "agent_message", "seq": 7, "sid": "s",
                              "msg_id": "m1", "data": _agent("r1", "v2")}, 130, 140, 25)
    ok = _check(out["m1"]["event_count"] == 1, "same-uid replace keeps count") and ok
    ok = _check(len(out["m1"]["last_events"]) == 1, "same-uid replace keeps list len") and ok

    # agent_message whose data has NO uuid is skipped (no count, no append).
    ing._update_summary_line(out, resolutions, root,
                             {"type": "agent_message", "seq": 8, "sid": "s",
                              "msg_id": "m1",
                              "data": {"type": "assistant", "message": {"content": []}}},
                             150, 160, 25)
    ok = _check(out["m1"]["event_count"] == 1, "no-uid render event skipped") and ok

    # Non-int seq: rec still created + render event counted, but seq bounds
    # are not updated past the setdefault value.
    ing._update_summary_line(out, resolutions, root,
                             {"type": "agent_message", "seq": "notint", "sid": "s",
                              "msg_id": "m-noint", "data": _agent("ni1")}, 170, 180, 25)
    ok = _check(out.get("m-noint", {}).get("event_count") == 1,
                "non-int seq still counts render event", f"{out.get('m-noint')}") and ok

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
    ok = _check(out["m-evict"]["event_count"] == 1 and len(out["m-evict"]["last_events"]) == 1,
                "evicted-uid replace leaves count+list unchanged") and ok
    return ok


# --------------------------------------------------------------------------- #
# Incremental summaries append (warm cache -> _append_summaries path)
# --------------------------------------------------------------------------- #
def test_incremental_summaries_append() -> bool:
    ok = True
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
    ok = _check(rec["event_count"] == 2, "incremental append picked up new event past junk",
                f"{rec['event_count']}") and ok
    ok = _check(ing._summaries_cache[root][0] >= new_size,
                "cache high-water advanced", f"{ing._summaries_cache[root][0]}")
    return ok


# --------------------------------------------------------------------------- #
# Cold scan + _rebuild_seq_offsets_locked (no sidecar, fresh ingester)
# --------------------------------------------------------------------------- #
def test_cold_scan_summaries_and_rebuild_offsets() -> bool:
    ok = True
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
    ok = _check(rec.get("event_count") == 2, "cold full scan built summaries",
                f"{rec.get('event_count')}") and ok
    ok = _check(ing._seq_offsets.get(root) is not None
                and len(ing._seq_offsets[root]) == 2,
                "cold scan populated _seq_offsets") and ok

    # _rebuild_seq_offsets_locked re-derives offsets from disk and resets _seq.
    ing._seq_offsets[root] = [999]                                  # corrupt
    ing._rebuild_seq_offsets_locked(pathlib.Path(_events_path(root)), root)
    ok = _check(len(ing._seq_offsets[root]) == 2, "rebuild_offsets restored 2 offsets",
                f"{len(ing._seq_offsets[root])}") and ok
    ok = _check(ing._seq[root] == 2, "rebuild_offsets reset _seq count") and ok
    return ok


# --------------------------------------------------------------------------- #
# _scan_summaries full-scan-cache fast path (full_scan_cache matches file)
# --------------------------------------------------------------------------- #
def test_scan_summaries_uses_full_scan_cache() -> bool:
    """When _full_scan_cache matches the file size and offsets are aligned,
    _scan_summaries reuses parsed entries instead of re-reading the file."""
    ok = True
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
    ok = _check(out.get("m1", {}).get("event_count") == 1,
                "scan_summaries built summary from cached entries", f"{out}") and ok
    ok = _check(id(ing._seq_offsets[root]) == offsets_identity,
                "fast path reused _seq_offsets without reassigning") and ok
    return ok


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
