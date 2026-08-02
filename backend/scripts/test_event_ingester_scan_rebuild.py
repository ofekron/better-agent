"""Unit coverage for the cold-scan rebuild + projection cluster of
event_ingester.py:

  - Cold `_scan_max_seq` full rebuild driven through every accessor's
    cache-miss fallback: max_seq_by_sid / render_seq_by_sid /
    session_event_meta / max_seq_for_sid / render_seq_for_sid /
    root_events_by_sid / root_events_version, plus the file-not-exist
    branch and the session_event_meta cursor fallback.
  - `_parse_events_range` blank-line + bad-JSON skip branches.
  - `_scan_max_seq` loop partials (out-of-order seq, render/ non-render,
    ownership resolution folding) + resolved-seq add.
  - `_build_root_events_projection` (stamped vs orphan, resolved skip,
    dup-uid skip, manager inner-dict frontend shape, non-str sid /
    non-render / metadata skips).
  - `_update_root_events_cache_for_entry` (non-str sid, ownership pop,
    stamped-uid remove, existing-dup skip, new-orphan append).
  - `_remove_root_event_projection` (empty / non-dict kept / uid match /
    all-removed pop).
  - `latest_render_event_uid` (with + without sid_filter).
  - `worker_event_rows` (dict row + non-dict defensive skip + sid_filter).
  - `cached_rows_for_byte_range` byte_end break.
  - `root_events_by_sid` version==0 short-circuit.

Run with:
    cd backend && .venv/bin/python scripts/test_event_ingester_scan_rebuild.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ingester-scan-")

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


def _fresh() -> EventIngester:
    return EventIngester()


def _events_path(root_id: str) -> str:
    return os.path.join(_TMP_HOME, "sessions", root_id, "events.jsonl")


def _write_raw(root_id: str, body: bytes) -> str:
    path = _events_path(root_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(body)
    return path


def _line(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def _agent(uid: str, seq: int, sid: str = "s1", msg_id: str | None = "m1") -> dict:
    e: dict = {"seq": seq, "sid": sid, "type": "agent_message",
               "data": {"uuid": uid, "type": "text",
                        "message": {"content": [{"type": "text", "text": "hi"}]}}}
    if msg_id is not None:
        e["msg_id"] = msg_id
    return e


def _manager(uid: str, seq: int, sid: str = "s1", msg_id: str | None = None) -> dict:
    # data.event is a dict -> _root_event_frontend_shape returns the inner.
    e: dict = {"seq": seq, "sid": sid, "type": "manager_event",
               "data": {"uuid": uid, "event": {"type": "manager_event",
                        "uuid": uid, "data": {"k": seq}}}}
    if msg_id is not None:
        e["msg_id"] = msg_id
    return e


# The rich cold-scan fixture. File order is deliberate (blank, bad-JSON,
# out-of-order seqs) to hit the parse-skip + loop-partial branches.
def _rich_body() -> bytes:
    parts = [
        _line(_agent("u1", seq=1, msg_id="m1")),       # stamped render+root
        b"\n",                                           # blank -> parse skip
        b"{this-is-bad-json\n",                          # bad JSON -> parse skip
        _line(_manager("u2", seq=2)),                    # candidate A (unresolved)
        _line({"seq": 3, "sid": "s1", "type": "worker_event", "msg_id": "m1",
               "data": {"delegation_id": "d1", "uuid": "w1"}}),
        _line(_manager("u3", seq=6)),                    # candidate B (resolved below)
        _line({"seq": 4, "sid": "s1", "type": "event_ownership_resolved",
               "data": {"event_seq": 6, "message_id": "m1"}}),
        _line({"seq": 5, "sid": "s1", "type": "user_message", "msg_id": "m1",
               "data": {"uuid": "u5"}}),                # non-render, non-root
        _line(_agent("u9", seq=0, msg_id="m9")),         # out-of-order seq (< max)
    ]
    return b"".join(parts)


# --------------------------------------------------------------------------- #
# Cold _scan_max_seq full rebuild via max_seq_by_sid
# --------------------------------------------------------------------------- #
def test_scan_max_seq_cold_rebuild() -> bool:
    ok = True
    root = "scan-cold"
    _write_raw(root, _rich_body())
    ing = _fresh()
    out = ing.max_seq_by_sid(root)
    ok = _check(out == {"s1": 6}, "max_seq_by_sid cold scan s1==6", f"{out}") and ok
    ok = _check(ing._render_seq_by_sid[root] == {"s1": 6},
                "render_seq_by_sid rebuilt s1==6",
                f"{ing._render_seq_by_sid.get(root)}") and ok
    # root_events_version counts agent/manager/ownership rows:
    # agent(1), managerA(2), ownership(4), managerB(6), agent-seq0(9) = 5.
    ok = _check(ing._root_events_version[root] == 5,
                "root_events_version == 5", f"{ing._root_events_version.get(root)}") and ok
    # candidates {2,6} minus resolved {6} -> 1.
    ok = _check(ing._root_events_candidate_version[root] == 1,
                "candidate_version == 1 (A unresolved, B resolved)",
                f"{ing._root_events_candidate_version.get(root)}") and ok
    # worker_rows indexed for d1.
    ok = _check("d1" in (ing._worker_rows.get(root) or {}),
                "worker_event indexed delegation d1",
                f"{ing._worker_rows.get(root)}") and ok
    # resolution folded into m1's summary bounds (seq 6 orphan folded in).
    summaries = ing._summaries_cache.get(root)
    ok = _check(summaries is not None and "m1" in summaries[1],
                "m1 summary present", f"{summaries}") and ok
    # Both sidecars written (meta + summaries).
    ok = _check(os.path.exists(os.path.join(_TMP_HOME, "sessions", root, "event_meta.json")),
                "event_meta sidecar written") and ok
    ok = _check(os.path.exists(os.path.join(_TMP_HOME, "sessions", root, "event_summaries.json")),
                "event_summaries sidecar written") and ok
    # root_events_cache populated via the candidate>0 path (projection built).
    rec = ing._root_events_cache.get(root)
    ok = _check(rec is not None and "s1" in rec[1],
                "root_events_cache projection built for s1", f"{rec}") and ok
    # Candidate A (seq 2) present, candidate B (seq 6) resolved-out.
    s1_events = rec[1]["s1"] if rec else []
    ok = _check(len(s1_events) == 1, "only unresolved candidate A in projection",
                f"{s1_events}") and ok
    ing._close_handle_locked(root)
    return ok


# --------------------------------------------------------------------------- #
# Accessor cache-miss fallbacks (each on a fresh ingester)
# --------------------------------------------------------------------------- #
def _simple_body() -> bytes:
    return _line(_agent("ua", seq=1, msg_id="ma")) + _line(_agent("ub", seq=2, msg_id="ma"))


def test_accessor_scan_fallbacks() -> bool:
    ok = True
    # render_seq_by_sid cold fallback (1091-1098).
    r = "acc-render"
    _write_raw(r, _simple_body())
    ing1 = _fresh()
    rs = ing1.render_seq_by_sid(r)
    ok = _check(rs == {"s1": 2}, "render_seq_by_sid fallback s1==2", f"{rs}") and ok

    # max_seq_for_sid cold fallback (1125-1132) + scalar return.
    r2 = "acc-maxsid"
    _write_raw(r2, _simple_body())
    ing2 = _fresh()
    ok = _check(ing2.max_seq_for_sid(r2, "s1") == 2,
                "max_seq_for_sid fallback == 2") and ok
    ok = _check(ing2.max_seq_for_sid(r2, "nope") == 0,
                "max_seq_for_sid unknown sid == 0") and ok

    # render_seq_for_sid cold fallback (1135-1142).
    r3 = "acc-rendersid"
    _write_raw(r3, _simple_body())
    ing3 = _fresh()
    ok = _check(ing3.render_seq_for_sid(r3, "s1") == 2,
                "render_seq_for_sid fallback == 2") and ok

    # root_events_version cold fallback (1611-1621).
    r4 = "acc-version"
    _write_raw(r4, _simple_body())
    ing4 = _fresh()
    ok = _check(ing4.root_events_version(r4) == 2,
                "root_events_version fallback == 2 (2 stamped agents)") and ok
    return ok


# --------------------------------------------------------------------------- #
# session_event_meta cursor fallback (1100-1118) + file-not-exist (1112-1113)
# --------------------------------------------------------------------------- #
def test_session_event_meta_cursor() -> bool:
    ok = True
    ing = _fresh()
    # max/render caches populated but _seq deliberately absent + file present
    # -> cursor counted from line count (1115-1117).
    r = "sem-cursor"
    _write_raw(r, _simple_body())
    ing._max_seq_by_sid[r] = {"s1": 2}
    ing._render_seq_by_sid[r] = {"s1": 2}
    has, cursor, render = ing.session_event_meta(r)
    ok = _check(has is True and cursor == 2 and render == {"s1": 2},
                "cursor counted from file when _seq absent",
                f"{has},{cursor},{render}") and ok

    # Same but file missing -> cursor 0 (1112-1113).
    ing2 = _fresh()
    r2 = "sem-nofile"
    ing2._max_seq_by_sid[r2] = {}
    ing2._render_seq_by_sid[r2] = {}
    has2, cursor2, _ = ing2.session_event_meta(r2)
    ok = _check(has2 is False and cursor2 == 0,
                "cursor == 0 when file missing", f"{has2},{cursor2}") and ok
    return ok


# --------------------------------------------------------------------------- #
# _scan_max_seq file-not-exist (1213-1217)
# --------------------------------------------------------------------------- #
def test_scan_file_not_exists() -> bool:
    ok = True
    ing = _fresh()
    out = ing.max_seq_by_sid("scan-noexist")
    ok = _check(out == {}, "missing file -> {} max_seq", f"{out}") and ok
    ok = _check(ing._root_events_version["scan-noexist"] == 0,
                "missing file -> root_events_version 0") and ok
    ok = _check(ing._root_events_candidate_version["scan-noexist"] == 0,
                "missing file -> candidate_version 0") and ok
    return ok


# --------------------------------------------------------------------------- #
# _scan_max_seq catch-up delta (1249): file "grows" between unlocked parse
# and the re-stat.
# --------------------------------------------------------------------------- #
def test_scan_max_seq_catchup_delta() -> bool:
    ok = True
    r = "scan-catchup"
    path = Path(_write_raw(r, _line(_agent("u1", seq=1, msg_id="m1"))))
    pre_size = path.stat().st_size
    # Append a real second line so the delta read is valid; the file now
    # genuinely has seq 2 bytes that the snapshot won't see.
    with open(path, "ab") as fh:
        fh.write(_line(_agent("u2", seq=2, msg_id="m2")))
    grown_size = path.stat().st_size
    ing = _fresh()
    lock = ing._locks.setdefault(r, __import__("threading").Lock())
    real_stat = Path.stat
    # Inside _scan_max_seq the Path.stat call order on this root is:
    #   #1 _event_file_signature (sidecar loader) -> real size is fine
    #   #2 snapshot_size = path.stat().st_size       -> must see PRE-grow size
    #   #3 post-parse re-stat (catch-up gate)        -> must see GROWN size
    # Intercept call #2 only so the catch-up delta branch (1249) fires.
    stat_calls = {"n": 0}

    def _stat_patch(self, *a, **k):  # noqa: ANN001
        stat_calls["n"] += 1
        st = real_stat(self, *a, **k)
        if stat_calls["n"] == 2:
            class _S:
                st_size = pre_size
                st_dev = st.st_dev
                st_ino = st.st_ino
                st_mtime_ns = st.st_mtime_ns
            return _S()
        return st

    ing._locks[r] = lock
    with lock:
        orig = Path.stat
        Path.stat = _stat_patch  # type: ignore[assignment]
        try:
            out = ing._scan_max_seq(r)
        finally:
            Path.stat = orig  # type: ignore[assignment]
    ok = _check(out == {"s1": 2}, "catch-up delta scan sees appended seq 2",
                f"{out}") and ok
    ok = _check(grown_size > pre_size, "fixture genuinely grew") and ok
    ing._close_handle_locked(r)
    return ok


# --------------------------------------------------------------------------- #
# _build_root_events_projection (direct): all branches
# --------------------------------------------------------------------------- #
def test_build_root_events_projection() -> bool:
    ok = True
    rows = [
        # non-str sid -> skipped (1661).
        {"seq": 1, "sid": 123, "type": "agent_message", "msg_id": "m1",
         "data": {"uuid": "x"}},
        # non-render type -> skipped (1663).
        {"seq": 2, "sid": "s1", "type": "user_message", "data": {"uuid": "y"}},
        # metadata agent -> skipped (1665).
        {"seq": 3, "sid": "s1", "type": "agent_message", "msg_id": "m1",
         "data": {"type": "ai-title"}},
        # stamped agent -> stamped[s1] = {a1} (1668-1670).
        {"seq": 4, "sid": "s1", "type": "agent_message", "msg_id": "m1",
         "data": {"uuid": "a1"}},
        # orphan candidate with uuid a1 (same as stamped) -> dup skip (1684).
        {"seq": 5, "sid": "s1", "type": "manager_event",
         "data": {"uuid": "a1", "event": {"type": "manager_event"}}},
        # orphan candidate fresh uuid a2 -> rendered (manager inner dict 1698).
        {"seq": 6, "sid": "s1", "type": "manager_event",
         "data": {"uuid": "a2", "event": {"type": "manager_event", "data": {"k": 1}}}},
        # orphan candidate resolved by ownership below -> skipped (1681).
        {"seq": 7, "sid": "s1", "type": "agent_message",
         "data": {"uuid": "a3"}},
        # ownership resolving seq 7.
        {"seq": 8, "sid": "s1", "type": "event_ownership_resolved",
         "data": {"event_seq": 7}},
    ]
    proj = EventIngester._build_root_events_projection(_fresh(), rows)
    ok = _check("s1" in proj, "s1 projection present", f"{proj}") and ok
    s1 = proj.get("s1", [])
    # Only a2 survives (a1 dup-of-stamped, a3 resolved-out).
    ok = _check(len(s1) == 1, "only fresh unresolved orphan rendered", f"{s1}") and ok
    # manager_event frontend shape returns the inner event dict.
    ok = _check(s1[0].get("type") == "manager_event",
                "manager orphan shaped to inner event", f"{s1[0]}") and ok
    return ok


# --------------------------------------------------------------------------- #
# _update_root_events_cache_for_entry (direct): all branches
# --------------------------------------------------------------------------- #
def test_update_root_events_cache_for_entry() -> bool:
    ok = True
    ing = _fresh()
    root = "upd-root"
    # Seed a cache with one existing orphan for s1 (uuid e1). Use an
    # agent_message-shaped event so data.uuid survives the frontend shape
    # (manager shapes collapse to the inner event and lose the uuid).
    shape = EventIngester._root_event_frontend_shape(
        {"type": "agent_message", "data": {"uuid": "e1"}})
    ing._root_events_cache[root] = (3, {"s1": [shape]})
    ing._root_events_version[root] = 3

    # non-str sid -> cache re-stamped, no mutation (1710-1711).
    ing._update_root_events_cache_for_entry(root, {"sid": 9, "type": "agent_message",
                                                   "data": {"uuid": "z"}})
    ok = _check(ing._root_events_cache[root][1] == {"s1": [shape]},
                "non-str sid leaves projection unchanged") and ok

    # event_ownership_resolved (with str sid) -> cache popped (1712-1713).
    ing._update_root_events_cache_for_entry(
        root, {"sid": "s1", "type": "event_ownership_resolved",
               "data": {"event_seq": 1}})
    ok = _check(root not in ing._root_events_cache,
                "ownership_resolved pops cache") and ok

    # Re-seed; stamped msg_id with uid removes matching orphan (1716-1721).
    ing._root_events_cache[root] = (3, {"s1": [shape]})
    ing._update_root_events_cache_for_entry(
        root, {"sid": "s1", "type": "agent_message", "msg_id": "m1",
               "data": {"uuid": "e1"}})
    ok = _check(ing._root_events_cache[root][1].get("s1", []) == [],
                "stamped uid removes matching orphan -> empty s1",
                f"{ing._root_events_cache[root][1]}") and ok

    # existing dup uid orphan -> skipped (1724-1730).
    ing._root_events_cache[root] = (3, {"s1": [shape]})
    ing._update_root_events_cache_for_entry(
        root, {"sid": "s1", "type": "agent_message",
               "data": {"uuid": "e1"}})
    ok = _check(len(ing._root_events_cache[root][1]["s1"]) == 1,
                "dup-uid orphan skipped (no append)") and ok

    # fresh orphan -> appended (1731).
    ing._update_root_events_cache_for_entry(
        root, {"sid": "s1", "type": "agent_message",
               "data": {"uuid": "e2"}})
    ok = _check(len(ing._root_events_cache[root][1]["s1"]) == 2,
                "fresh orphan appended") and ok

    # cached is None -> early return (1704-1705).
    ing2 = _fresh()
    ing2._update_root_events_cache_for_entry("none-root",
                                             {"sid": "s1", "type": "agent_message",
                                              "data": {"uuid": "q"}})
    ok = _check(True, "no-cache early return did not raise") and ok
    return ok


# --------------------------------------------------------------------------- #
# _remove_root_event_projection (direct): all branches
# --------------------------------------------------------------------------- #
def test_remove_root_event_projection() -> bool:
    ok = True
    proj: dict[str, list[dict]] = {}
    ing = _fresh()
    # empty/missing list -> no-op (1742).
    ing._remove_root_event_projection(proj, "s1", uid="x")
    ok = _check("s1" not in proj, "missing sid list -> no-op") and ok

    # mix: non-dict kept (1746-1748), uid match removed (1750), result non-empty.
    proj["s1"] = [{"data": {"uuid": "keep"}}, "not-a-dict", {"data": {"uuid": "rm"}}]
    ing._remove_root_event_projection(proj, "s1", uid="rm")
    ok = _check(proj["s1"] == [{"data": {"uuid": "keep"}}, "not-a-dict"],
                "uid match removed, non-dict kept", f"{proj['s1']}") and ok

    # all removed -> sid popped (1753-1756).
    proj["s2"] = [{"data": {"uuid": "only"}}]
    ing._remove_root_event_projection(proj, "s2", uid="only")
    ok = _check("s2" not in proj, "all removed -> sid popped") and ok

    # no uid given -> nothing removed (1750 False).
    proj["s3"] = [{"data": {"uuid": "a"}}]
    ing._remove_root_event_projection(proj, "s3")
    ok = _check(proj["s3"] == [{"data": {"uuid": "a"}}],
                "no uid -> list unchanged") and ok
    return ok


# --------------------------------------------------------------------------- #
# latest_render_event_uid (with + without sid_filter)
# --------------------------------------------------------------------------- #
def test_latest_render_event_uid() -> bool:
    ok = True
    root = "latest"
    _write_raw(root, _line(_agent("u1", seq=1, msg_id="m1"))
               + _line(_agent("u2", seq=2, msg_id="m1")))
    ing = _fresh()
    ing.max_seq_by_sid(root)  # warm summaries via cold scan
    # Without sid_filter: scans all summaries, returns highest-seq uid.
    uid = ing.latest_render_event_uid(root)
    ok = _check(uid == "u2", "latest uid == u2 (no filter)", f"{uid}") and ok
    # With sid_filter: cached after first call (2068-2069).
    uid_f = ing.latest_render_event_uid(root, sid_filter="s1")
    ok = _check(uid_f == "u2", "latest uid == u2 (sid_filter)", f"{uid_f}") and ok
    ok = _check(root in ing._latest_render_uid_by_sid,
                "sid_filter result cached") and ok
    # Unknown sid_filter -> None.
    ok = _check(ing.latest_render_event_uid(root, sid_filter="nope") is None,
                "unknown sid_filter -> None") and ok
    ing._close_handle_locked(root)
    return ok


# --------------------------------------------------------------------------- #
# worker_event_rows (dict row + non-dict defensive skip + sid_filter)
# --------------------------------------------------------------------------- #
def test_worker_event_rows() -> bool:
    ok = True
    root = "worker"
    # A valid worker_event line; the scan indexes its d1 span.
    wline = _line({"seq": 1, "sid": "s1", "type": "worker_event",
                   "data": {"delegation_id": "d1", "uuid": "w1"}})
    _write_raw(root, wline)
    ing = _fresh()
    ing.max_seq_by_sid(root)  # indexes d1 span from the worker_event line
    rows = ing.worker_event_rows(root, {"d1"})
    ok = _check("d1" in rows and len(rows["d1"]) == 1,
                "d1 dict row returned", f"{rows}") and ok
    # sid_filter mismatch excludes the d1 row (2114).
    rows_filt = ing.worker_event_rows(root, {"d1"}, sid_filter="other")
    ok = _check(rows_filt == {}, "sid_filter mismatch excludes row",
                f"{rows_filt}") and ok
    ing._close_handle_locked(root)
    return ok


# --------------------------------------------------------------------------- #
# cached_rows_for_byte_range byte_end break (1583-1584)
# --------------------------------------------------------------------------- #
def test_cached_rows_for_byte_range() -> bool:
    ok = True
    root = "byte-range"
    _write_raw(root, _line(_agent("u1", seq=1, msg_id="m1"))
               + _line(_agent("u2", seq=2, msg_id="m2")))
    ing = _fresh()
    ing.max_seq_by_sid(root)  # warms full_scan_cache + seq_offsets
    offsets = ing._seq_offsets[root]
    # Range ending exactly at line 2's start -> only line 1 returned (break).
    rows = ing.cached_rows_for_byte_range(root, 0, offsets[1])
    ok = _check(rows is not None and len(rows) == 1,
                "byte_end at line2 start -> 1 row (break)", f"{rows}") and ok
    # byte_end <= byte_start -> [].
    ok = _check(ing.cached_rows_for_byte_range(root, 5, 5) == [],
                "byte_end<=byte_start -> []") and ok
    ing._close_handle_locked(root)
    return ok


# --------------------------------------------------------------------------- #
# root_events_by_sid version==0 short-circuit (1603-1604)
# --------------------------------------------------------------------------- #
def test_root_events_by_sid_version_zero() -> bool:
    ok = True
    root = "rev-zero"
    # Only user_message events -> no root-projection rows -> version 0.
    _write_raw(root, _line({"seq": 1, "sid": "s1", "type": "user_message",
                            "msg_id": "m1", "data": {"uuid": "u1"}}))
    ing = _fresh()
    out = ing.root_events_by_sid(root)
    ok = _check(out == {}, "version==0 -> empty projection", f"{out}") and ok
    ok = _check(ing._root_events_version[root] == 0,
                "root_events_version == 0", f"{ing._root_events_version.get(root)}") and ok
    ing._close_handle_locked(root)
    return ok


# --------------------------------------------------------------------------- #
# Accessor cache-hit fast paths (1095, 1139) — call twice after a scan
# --------------------------------------------------------------------------- #
def test_accessor_cache_hits() -> bool:
    ok = True
    root = "acc-hit"
    _write_raw(root, _simple_body())
    ing = _fresh()
    ing.max_seq_by_sid(root)  # warm all caches via cold scan
    # Second calls hit the cache-hit returns (1095, 1139).
    ok = _check(ing.render_seq_by_sid(root) == {"s1": 2},
                "render_seq_by_sid cache-hit", ) and ok
    ok = _check(ing.render_seq_for_sid(root, "s1") == 2,
                "render_seq_for_sid cache-hit") and ok
    ok = _check(ing.max_seq_by_sid(root) == {"s1": 2},
                "max_seq_by_sid cache-hit") and ok
    ok = _check(ing.max_seq_for_sid(root, "s1") == 2,
                "max_seq_for_sid cache-hit") and ok
    ing._close_handle_locked(root)
    return ok


# --------------------------------------------------------------------------- #
# _read_all_events_locked cached fast path (1628)
# --------------------------------------------------------------------------- #
def test_read_all_events_locked_cached() -> bool:
    ok = True
    root = "readall-cached"
    body = _simple_body()
    path = Path(_write_raw(root, body))
    file_size = path.stat().st_size
    ing = _fresh()
    # Warm the full-scan cache at the current file size.
    ing._full_scan_cache[root] = (file_size, [{"seq": 1}, {"seq": 2}])
    rows = ing._read_all_events_locked(path, root, file_size)
    ok = _check(rows == [{"seq": 1}, {"seq": 2}],
                "cached[0]==file_size returns cached entries", f"{rows}") and ok
    return ok


# --------------------------------------------------------------------------- #
# root_events_by_sid version==0 short-circuit via injected version (1603-1604)
# --------------------------------------------------------------------------- #
def test_root_events_by_sid_version_zero_injected() -> bool:
    ok = True
    root = "rev-inject"
    _write_raw(root, _simple_body())
    ing = _fresh()
    # Version populated but cache absent + candidate_version 0 -> the
    # version==0 short-circuit (1602-1604) without a scan seeding the cache.
    ing._root_events_version[root] = 0
    ing._root_events_candidate_version[root] = 0
    out = ing.root_events_by_sid(root)
    ok = _check(out == {}, "injected version 0 -> empty projection", f"{out}") and ok
    ok = _check(ing._root_events_cache[root] == (0, {}),
                "cache stamped to (0, {})", f"{ing._root_events_cache.get(root)}") and ok
    return ok


# --------------------------------------------------------------------------- #
# _root_event_frontend_shape: manager inner-dict vs fallback (1696-1700)
# --------------------------------------------------------------------------- #
def test_root_event_frontend_shape() -> bool:
    ok = True
    S = EventIngester._root_event_frontend_shape
    # manager_event with dict data.event -> inner returned (1698-1699).
    mgr = S({"type": "manager_event",
             "data": {"event": {"type": "manager_event", "data": {"k": 1}}}})
    ok = _check(mgr == {"type": "manager_event", "data": {"k": 1}},
                "manager inner dict returned", f"{mgr}") and ok
    # manager_event WITHOUT dict data.event -> agent fallback (1700).
    mgr2 = S({"type": "manager_event", "data": {"uuid": "x"}})
    ok = _check(mgr2 == {"type": "agent_message", "data": {"uuid": "x"}},
                "manager no-inner -> agent fallback", f"{mgr2}") and ok
    # agent_message -> agent fallback (1700).
    ag = S({"type": "agent_message", "data": {"uuid": "y"}})
    ok = _check(ag == {"type": "agent_message", "data": {"uuid": "y"}},
                "agent -> agent shape", f"{ag}") and ok
    return ok


# --------------------------------------------------------------------------- #
# latest_render_event_uid malformed-event skip branches (2058-2069)
# --------------------------------------------------------------------------- #
def test_latest_render_event_uid_skips() -> bool:
    ok = True
    root = "latest-skips"
    _write_raw(root, b"")  # empty file: path exists, no competing scan rows
    ing = _fresh()
    # _summaries_state's cache-hit gate also requires the seq index to be
    # current (_seq_offsets/_next_offset/_seq consistent with file size 0).
    ing._seq_offsets[root] = []
    ing._next_offset[root] = 0
    ing._seq[root] = 0
    # Inject summaries at byte_end 0 (== file size) so _summaries_state
    # treats the cache as warm and surfaces these crafted last_events.
    # mA sets latest=(10,a10); mB (reversed) hits every skip then a uid
    # whose seq is NOT > latest (2065 False).
    ing._summaries_cache[root] = (0, {
        "mA": {"sid": None, "last_events": [{"seq": 10, "data": {"uuid": "a10"}}]},
        "mB": {"sid": None, "last_events": [
            {"seq": 3, "data": {"uuid": "b3"}},
            {"seq": 11, "data": {"uuid": "b11"}},
            {"seq": 5, "data": {}},            # no uid -> 2064 skip
            "not-a-dict",                       # -> 2058 skip
            {"seq": "x", "data": {}},           # non-int seq -> 2061 skip
        ]},
        # mC's first reversed uid (seq 2) is < latest (10) -> 2065 False.
        "mC": {"sid": None, "last_events": [{"seq": 2, "data": {"uuid": "c2"}}]},
    }, {})
    uid = ing.latest_render_event_uid(root)
    # Highest seq uid across both summaries is b11 (seq 11 > a10 seq 10).
    ok = _check(uid == "b11", "latest uid == b11 (highest seq)", f"{uid}") and ok
    return ok


# --------------------------------------------------------------------------- #
# _update_summary_line / _fold_resolutions non-int seq_start (2276, 2322-2323)
# --------------------------------------------------------------------------- #
def test_summary_line_non_int_seq_start() -> bool:
    ok = True
    root = "seqstart"
    # Contiguous line positions so _seq_byte_range can resolve the orphan.
    # L1: mN event with NO seq -> rec seq_start stays non-int.
    # L2: orphan agent seq=2 (no msg_id) -> a resolvable journal seq.
    # L3: ownership event_seq=2 message_id=mN -> fold hits the non-int
    #     seq_start else-branch (2276).
    # L4: mP event with NO seq -> rec seq_start non-int.
    # L5: mP event seq=5 -> second update hits the seq_start int-set (2323).
    parts = [
        _line({"sid": "s1", "type": "agent_message", "msg_id": "mN",
               "data": {"uuid": "n1", "type": "text",
                        "message": {"content": [{"type": "text", "text": "a"}]}}}),
        _line({"seq": 2, "sid": "s1", "type": "agent_message",
               "data": {"uuid": "o2"}}),
        _line({"seq": 3, "sid": "s1", "type": "event_ownership_resolved",
               "data": {"event_seq": 2, "message_id": "mN"}}),
        _line({"sid": "s1", "type": "agent_message", "msg_id": "mP",
               "data": {"uuid": "p1", "type": "text",
                        "message": {"content": [{"type": "text", "text": "b"}]}}}),
        _line({"seq": 5, "sid": "s1", "type": "agent_message", "msg_id": "mP",
               "data": {"uuid": "p2", "type": "text",
                        "message": {"content": [{"type": "text", "text": "c"}]}}}),
    ]
    _write_raw(root, b"".join(parts))
    ing = _fresh()
    ing.max_seq_by_sid(root)  # full scan drives _update_summary_line + _fold_resolutions
    summaries = ing._summaries_cache[root][1]
    # mN folded: seq_start set to the resolved orphan seq (2).
    ok = _check(summaries.get("mN", {}).get("seq_start") == 2,
                "mN seq_start folded to orphan seq 2",
                f"{summaries.get('mN')}") and ok
    # mP second event set seq_start to its int seq (5).
    ok = _check(summaries.get("mP", {}).get("seq_start") == 5,
                "mP seq_start set to int seq 5",
                f"{summaries.get('mP')}") and ok
    ing._close_handle_locked(root)
    return ok


# --------------------------------------------------------------------------- #
# _scan_summaries cached-entries fast path (2436)
# --------------------------------------------------------------------------- #
def test_scan_summaries_cached_path() -> bool:
    ok = True
    root = "scan-summ-cached"
    path = Path(_write_raw(root, _simple_body()))
    ing = _fresh()
    ing.max_seq_by_sid(root)  # warms _full_scan_cache + _seq_offsets at file_size
    # Direct call takes the cached-entries branch (full_scan_cache matches
    # file_size) instead of re-reading the file -> line_end via offsets (2436).
    summaries, _ = ing._scan_summaries(path, root, 25)
    ok = _check("ma" in summaries, "cached-path rebuilt ma summary",
                f"{list(summaries)}") and ok
    ing._close_handle_locked(root)
    return ok


TESTS = [
    test_scan_max_seq_cold_rebuild,
    test_accessor_scan_fallbacks,
    test_session_event_meta_cursor,
    test_scan_file_not_exists,
    test_scan_max_seq_catchup_delta,
    test_build_root_events_projection,
    test_update_root_events_cache_for_entry,
    test_remove_root_event_projection,
    test_latest_render_event_uid,
    test_worker_event_rows,
    test_cached_rows_for_byte_range,
    test_root_events_by_sid_version_zero,
    test_accessor_cache_hits,
    test_read_all_events_locked_cached,
    test_root_events_by_sid_version_zero_injected,
    test_root_event_frontend_shape,
    test_latest_render_event_uid_skips,
    test_summary_line_non_int_seq_start,
    test_scan_summaries_cached_path,
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
