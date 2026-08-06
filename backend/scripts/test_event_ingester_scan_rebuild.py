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
def test_scan_max_seq_cold_rebuild() -> None:
    root = "scan-cold"
    ing = _fresh()
    try:
        _write_raw(root, _rich_body())
        out = ing.max_seq_by_sid(root)
        assert out == {"s1": 6}, f"max_seq_by_sid cold scan s1==6 -- {out}"
        assert ing._render_seq_by_sid[root] == {"s1": 6}, \
            f"render_seq_by_sid rebuilt s1==6 -- {ing._render_seq_by_sid.get(root)}"
        # root_events_version counts agent/manager/ownership rows:
        # agent(1), managerA(2), ownership(4), managerB(6), agent-seq0(9) = 5.
        assert ing._root_events_version[root] == 5, \
            f"root_events_version == 5 -- {ing._root_events_version.get(root)}"
        # candidates {2,6} minus resolved {6} -> 1.
        assert ing._root_events_candidate_version[root] == 1, \
            f"candidate_version == 1 (A unresolved, B resolved) -- {ing._root_events_candidate_version.get(root)}"
        # worker_rows indexed for d1.
        assert "d1" in (ing._worker_rows.get(root) or {}), \
            f"worker_event indexed delegation d1 -- {ing._worker_rows.get(root)}"
        # resolution folded into m1's summary bounds (seq 6 orphan folded in).
        summaries = ing._summaries_cache.get(root)
        assert summaries is not None and "m1" in summaries[1], f"m1 summary present -- {summaries}"
        # Both sidecars written (meta + summaries).
        assert os.path.exists(os.path.join(_TMP_HOME, "sessions", root, "event_meta.json")), \
            "event_meta sidecar written"
        assert os.path.exists(os.path.join(_TMP_HOME, "sessions", root, "event_summaries.json")), \
            "event_summaries sidecar written"
        # root_events_cache populated via the candidate>0 path (projection built).
        rec = ing._root_events_cache.get(root)
        assert rec is not None and "s1" in rec[1], \
            f"root_events_cache projection built for s1 -- {rec}"
        # Candidate A (seq 2) present, candidate B (seq 6) resolved-out.
        s1_events = rec[1]["s1"] if rec else []
        assert len(s1_events) == 1, f"only unresolved candidate A in projection -- {s1_events}"
    finally:
        ing._close_handle_locked(root)


# --------------------------------------------------------------------------- #
# Accessor cache-miss fallbacks (each on a fresh ingester)
# --------------------------------------------------------------------------- #
def _simple_body() -> bytes:
    return _line(_agent("ua", seq=1, msg_id="ma")) + _line(_agent("ub", seq=2, msg_id="ma"))


def test_accessor_scan_fallbacks() -> None:
    # render_seq_by_sid cold fallback (1091-1098).
    r = "acc-render"
    _write_raw(r, _simple_body())
    ing1 = _fresh()
    rs = ing1.render_seq_by_sid(r)
    assert rs == {"s1": 2}, f"render_seq_by_sid fallback s1==2 -- {rs}"

    # max_seq_for_sid cold fallback (1125-1132) + scalar return.
    r2 = "acc-maxsid"
    _write_raw(r2, _simple_body())
    ing2 = _fresh()
    assert ing2.max_seq_for_sid(r2, "s1") == 2, "max_seq_for_sid fallback == 2"
    assert ing2.max_seq_for_sid(r2, "nope") == 0, "max_seq_for_sid unknown sid == 0"

    # render_seq_for_sid cold fallback (1135-1142).
    r3 = "acc-rendersid"
    _write_raw(r3, _simple_body())
    ing3 = _fresh()
    assert ing3.render_seq_for_sid(r3, "s1") == 2, "render_seq_for_sid fallback == 2"

    # root_events_version cold fallback (1611-1621).
    r4 = "acc-version"
    _write_raw(r4, _simple_body())
    ing4 = _fresh()
    assert ing4.root_events_version(r4) == 2, \
        "root_events_version fallback == 2 (2 stamped agents)"


# --------------------------------------------------------------------------- #
# session_event_meta cursor fallback (1100-1118) + file-not-exist (1112-1113)
# --------------------------------------------------------------------------- #
def test_session_event_meta_cursor() -> None:
    ing = _fresh()
    # max/render caches populated but _seq deliberately absent + file present
    # -> cursor counted from line count (1115-1117).
    r = "sem-cursor"
    _write_raw(r, _simple_body())
    ing._max_seq_by_sid[r] = {"s1": 2}
    ing._render_seq_by_sid[r] = {"s1": 2}
    has, cursor, render = ing.session_event_meta(r)
    assert has is True and cursor == 2 and render == {"s1": 2}, \
        f"cursor counted from file when _seq absent -- {has},{cursor},{render}"

    # Same but file missing -> cursor 0 (1112-1113).
    ing2 = _fresh()
    r2 = "sem-nofile"
    ing2._max_seq_by_sid[r2] = {}
    ing2._render_seq_by_sid[r2] = {}
    has2, cursor2, _ = ing2.session_event_meta(r2)
    assert has2 is False and cursor2 == 0, f"cursor == 0 when file missing -- {has2},{cursor2}"


# --------------------------------------------------------------------------- #
# _scan_max_seq file-not-exist (1213-1217)
# --------------------------------------------------------------------------- #
def test_scan_file_not_exists() -> None:
    ing = _fresh()
    out = ing.max_seq_by_sid("scan-noexist")
    assert out == {}, f"missing file -> {{}} max_seq -- {out}"
    assert ing._root_events_version["scan-noexist"] == 0, "missing file -> root_events_version 0"
    assert ing._root_events_candidate_version["scan-noexist"] == 0, \
        "missing file -> candidate_version 0"


# --------------------------------------------------------------------------- #
# _scan_max_seq catch-up delta (1249): file "grows" between unlocked parse
# and the re-stat.
# --------------------------------------------------------------------------- #
def test_scan_max_seq_catchup_delta() -> None:
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
    try:
        with lock:
            orig = Path.stat
            Path.stat = _stat_patch  # type: ignore[assignment]
            try:
                out = ing._scan_max_seq(r)
            finally:
                Path.stat = orig  # type: ignore[assignment]
        assert out == {"s1": 2}, f"catch-up delta scan sees appended seq 2 -- {out}"
        assert grown_size > pre_size, "fixture genuinely grew"
    finally:
        ing._close_handle_locked(r)


# --------------------------------------------------------------------------- #
# _build_root_events_projection (direct): all branches
# --------------------------------------------------------------------------- #
def test_build_root_events_projection() -> None:
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
    assert "s1" in proj, f"s1 projection present -- {proj}"
    s1 = proj.get("s1", [])
    # Only a2 survives (a1 dup-of-stamped, a3 resolved-out).
    assert len(s1) == 1, f"only fresh unresolved orphan rendered -- {s1}"
    # manager_event frontend shape returns the inner event dict.
    assert s1[0].get("type") == "manager_event", \
        f"manager orphan shaped to inner event -- {s1[0]}"


# --------------------------------------------------------------------------- #
# _update_root_events_cache_for_entry (direct): all branches
# --------------------------------------------------------------------------- #
def test_update_root_events_cache_for_entry() -> None:
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
    assert ing._root_events_cache[root][1] == {"s1": [shape]}, \
        "non-str sid leaves projection unchanged"

    # event_ownership_resolved (with str sid) -> cache popped (1712-1713).
    ing._update_root_events_cache_for_entry(
        root, {"sid": "s1", "type": "event_ownership_resolved",
               "data": {"event_seq": 1}})
    assert root not in ing._root_events_cache, "ownership_resolved pops cache"

    # Re-seed; stamped msg_id with uid removes matching orphan (1716-1721).
    ing._root_events_cache[root] = (3, {"s1": [shape]})
    ing._update_root_events_cache_for_entry(
        root, {"sid": "s1", "type": "agent_message", "msg_id": "m1",
               "data": {"uuid": "e1"}})
    assert ing._root_events_cache[root][1].get("s1", []) == [], \
        f"stamped uid removes matching orphan -> empty s1 -- {ing._root_events_cache[root][1]}"

    # existing dup uid orphan -> skipped (1724-1730).
    ing._root_events_cache[root] = (3, {"s1": [shape]})
    ing._update_root_events_cache_for_entry(
        root, {"sid": "s1", "type": "agent_message",
               "data": {"uuid": "e1"}})
    assert len(ing._root_events_cache[root][1]["s1"]) == 1, \
        "dup-uid orphan skipped (no append)"

    # fresh orphan -> appended (1731).
    ing._update_root_events_cache_for_entry(
        root, {"sid": "s1", "type": "agent_message",
               "data": {"uuid": "e2"}})
    assert len(ing._root_events_cache[root][1]["s1"]) == 2, "fresh orphan appended"

    # cached is None -> early return (1704-1705).
    ing2 = _fresh()
    ing2._update_root_events_cache_for_entry("none-root",
                                             {"sid": "s1", "type": "agent_message",
                                              "data": {"uuid": "q"}})
    assert "none-root" not in ing2._root_events_cache, "no-cache early return did not raise"


# --------------------------------------------------------------------------- #
# _remove_root_event_projection (direct): all branches
# --------------------------------------------------------------------------- #
def test_remove_root_event_projection() -> None:
    proj: dict[str, list[dict]] = {}
    ing = _fresh()
    # empty/missing list -> no-op (1742).
    ing._remove_root_event_projection(proj, "s1", uid="x")
    assert "s1" not in proj, "missing sid list -> no-op"

    # mix: non-dict kept (1746-1748), uid match removed (1750), result non-empty.
    proj["s1"] = [{"data": {"uuid": "keep"}}, "not-a-dict", {"data": {"uuid": "rm"}}]
    ing._remove_root_event_projection(proj, "s1", uid="rm")
    assert proj["s1"] == [{"data": {"uuid": "keep"}}, "not-a-dict"], \
        f"uid match removed, non-dict kept -- {proj['s1']}"

    # all removed -> sid popped (1753-1756).
    proj["s2"] = [{"data": {"uuid": "only"}}]
    ing._remove_root_event_projection(proj, "s2", uid="only")
    assert "s2" not in proj, "all removed -> sid popped"

    # no uid given -> nothing removed (1750 False).
    proj["s3"] = [{"data": {"uuid": "a"}}]
    ing._remove_root_event_projection(proj, "s3")
    assert proj["s3"] == [{"data": {"uuid": "a"}}], "no uid -> list unchanged"


# --------------------------------------------------------------------------- #
# latest_render_event_uid (with + without sid_filter)
# --------------------------------------------------------------------------- #
def test_latest_render_event_uid() -> None:
    root = "latest"
    ing = _fresh()
    try:
        _write_raw(root, _line(_agent("u1", seq=1, msg_id="m1"))
                   + _line(_agent("u2", seq=2, msg_id="m1")))
        ing.max_seq_by_sid(root)  # warm summaries via cold scan
        # Without sid_filter: scans all summaries, returns highest-seq uid.
        uid = ing.latest_render_event_uid(root)
        assert uid == "u2", f"latest uid == u2 (no filter) -- {uid}"
        # With sid_filter: cached after first call (2068-2069).
        uid_f = ing.latest_render_event_uid(root, sid_filter="s1")
        assert uid_f == "u2", f"latest uid == u2 (sid_filter) -- {uid_f}"
        assert root in ing._latest_render_uid_by_sid, "sid_filter result cached"
        # Unknown sid_filter -> None.
        assert ing.latest_render_event_uid(root, sid_filter="nope") is None, \
            "unknown sid_filter -> None"
    finally:
        ing._close_handle_locked(root)


# --------------------------------------------------------------------------- #
# worker_event_rows (dict row + non-dict defensive skip + sid_filter)
# --------------------------------------------------------------------------- #
def test_worker_event_rows() -> None:
    root = "worker"
    ing = _fresh()
    try:
        # A valid worker_event line; the scan indexes its d1 span.
        wline = _line({"seq": 1, "sid": "s1", "type": "worker_event",
                       "data": {"delegation_id": "d1", "uuid": "w1"}})
        _write_raw(root, wline)
        ing.max_seq_by_sid(root)  # indexes d1 span from the worker_event line
        rows = ing.worker_event_rows(root, {"d1"})
        assert "d1" in rows and len(rows["d1"]) == 1, f"d1 dict row returned -- {rows}"
        # sid_filter mismatch excludes the d1 row (2114).
        rows_filt = ing.worker_event_rows(root, {"d1"}, sid_filter="other")
        assert rows_filt == {}, f"sid_filter mismatch excludes row -- {rows_filt}"
    finally:
        ing._close_handle_locked(root)


# --------------------------------------------------------------------------- #
# cached_rows_for_byte_range byte_end break (1583-1584)
# --------------------------------------------------------------------------- #
def test_cached_rows_for_byte_range() -> None:
    root = "byte-range"
    ing = _fresh()
    try:
        _write_raw(root, _line(_agent("u1", seq=1, msg_id="m1"))
                   + _line(_agent("u2", seq=2, msg_id="m2")))
        ing.max_seq_by_sid(root)  # warms full_scan_cache + seq_offsets
        offsets = ing._seq_offsets[root]
        # Range ending exactly at line 2's start -> only line 1 returned (break).
        rows = ing.cached_rows_for_byte_range(root, 0, offsets[1])
        assert rows is not None and len(rows) == 1, \
            f"byte_end at line2 start -> 1 row (break) -- {rows}"
        # byte_end <= byte_start -> [].
        assert ing.cached_rows_for_byte_range(root, 5, 5) == [], "byte_end<=byte_start -> []"
    finally:
        ing._close_handle_locked(root)


# --------------------------------------------------------------------------- #
# root_events_by_sid version==0 short-circuit (1603-1604)
# --------------------------------------------------------------------------- #
def test_root_events_by_sid_version_zero() -> None:
    root = "rev-zero"
    ing = _fresh()
    try:
        # Only user_message events -> no root-projection rows -> version 0.
        _write_raw(root, _line({"seq": 1, "sid": "s1", "type": "user_message",
                                "msg_id": "m1", "data": {"uuid": "u1"}}))
        out = ing.root_events_by_sid(root)
        assert out == {}, f"version==0 -> empty projection -- {out}"
        assert ing._root_events_version[root] == 0, \
            f"root_events_version == 0 -- {ing._root_events_version.get(root)}"
    finally:
        ing._close_handle_locked(root)


# --------------------------------------------------------------------------- #
# Accessor cache-hit fast paths (1095, 1139) — call twice after a scan
# --------------------------------------------------------------------------- #
def test_accessor_cache_hits() -> None:
    root = "acc-hit"
    ing = _fresh()
    try:
        _write_raw(root, _simple_body())
        ing.max_seq_by_sid(root)  # warm all caches via cold scan
        # Second calls hit the cache-hit returns (1095, 1139).
        assert ing.render_seq_by_sid(root) == {"s1": 2}, "render_seq_by_sid cache-hit"
        assert ing.render_seq_for_sid(root, "s1") == 2, "render_seq_for_sid cache-hit"
        assert ing.max_seq_by_sid(root) == {"s1": 2}, "max_seq_by_sid cache-hit"
        assert ing.max_seq_for_sid(root, "s1") == 2, "max_seq_for_sid cache-hit"
    finally:
        ing._close_handle_locked(root)


# --------------------------------------------------------------------------- #
# _read_all_events_locked cached fast path (1628)
# --------------------------------------------------------------------------- #
def test_read_all_events_locked_cached() -> None:
    root = "readall-cached"
    body = _simple_body()
    path = Path(_write_raw(root, body))
    file_size = path.stat().st_size
    ing = _fresh()
    # Warm the full-scan cache at the current file size.
    ing._full_scan_cache[root] = (file_size, [{"seq": 1}, {"seq": 2}])
    rows = ing._read_all_events_locked(path, root, file_size)
    assert rows == [{"seq": 1}, {"seq": 2}], \
        f"cached[0]==file_size returns cached entries -- {rows}"


# --------------------------------------------------------------------------- #
# root_events_by_sid version==0 short-circuit via injected version (1603-1604)
# --------------------------------------------------------------------------- #
def test_root_events_by_sid_version_zero_injected() -> None:
    root = "rev-inject"
    _write_raw(root, _simple_body())
    ing = _fresh()
    # Version populated but cache absent + candidate_version 0 -> the
    # version==0 short-circuit (1602-1604) without a scan seeding the cache.
    ing._root_events_version[root] = 0
    ing._root_events_candidate_version[root] = 0
    out = ing.root_events_by_sid(root)
    assert out == {}, f"injected version 0 -> empty projection -- {out}"
    assert ing._root_events_cache[root] == (0, {}), \
        f"cache stamped to (0, {{}}) -- {ing._root_events_cache.get(root)}"


# --------------------------------------------------------------------------- #
# _root_event_frontend_shape: manager inner-dict vs fallback (1696-1700)
# --------------------------------------------------------------------------- #
def test_root_event_frontend_shape() -> None:
    S = EventIngester._root_event_frontend_shape
    # manager_event with dict data.event -> inner returned (1698-1699).
    mgr = S({"type": "manager_event",
             "data": {"event": {"type": "manager_event", "data": {"k": 1}}}})
    assert mgr == {"type": "manager_event", "data": {"k": 1}}, \
        f"manager inner dict returned -- {mgr}"
    # manager_event WITHOUT dict data.event -> agent fallback (1700).
    mgr2 = S({"type": "manager_event", "data": {"uuid": "x"}})
    assert mgr2 == {"type": "agent_message", "data": {"uuid": "x"}}, \
        f"manager no-inner -> agent fallback -- {mgr2}"
    # agent_message -> agent fallback (1700).
    ag = S({"type": "agent_message", "data": {"uuid": "y"}})
    assert ag == {"type": "agent_message", "data": {"uuid": "y"}}, \
        f"agent -> agent shape -- {ag}"


# --------------------------------------------------------------------------- #
# latest_render_event_uid malformed-event skip branches (2058-2069)
# --------------------------------------------------------------------------- #
def test_latest_render_event_uid_skips() -> None:
    root = "latest-skips"
    ing = _fresh()
    try:
        _write_raw(root, b"")  # empty file: path exists, no competing scan rows
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
        assert uid == "b11", f"latest uid == b11 (highest seq) -- {uid}"
    finally:
        ing._close_handle_locked(root)


# --------------------------------------------------------------------------- #
# _update_summary_line / _fold_resolutions non-int seq_start (2276, 2322-2323)
# --------------------------------------------------------------------------- #
def test_summary_line_non_int_seq_start() -> None:
    root = "seqstart"
    ing = _fresh()
    try:
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
        ing.max_seq_by_sid(root)  # full scan drives _update_summary_line + _fold_resolutions
        summaries = ing._summaries_cache[root][1]
        # mN folded: seq_start set to the resolved orphan seq (2).
        assert summaries.get("mN", {}).get("seq_start") == 2, \
            f"mN seq_start folded to orphan seq 2 -- {summaries.get('mN')}"
        # mP second event set seq_start to its int seq (5).
        assert summaries.get("mP", {}).get("seq_start") == 5, \
            f"mP seq_start set to int seq 5 -- {summaries.get('mP')}"
    finally:
        ing._close_handle_locked(root)


# --------------------------------------------------------------------------- #
# _scan_summaries cached-entries fast path (2436)
# --------------------------------------------------------------------------- #
def test_scan_summaries_cached_path() -> None:
    root = "scan-summ-cached"
    ing = _fresh()
    try:
        path = Path(_write_raw(root, _simple_body()))
        ing.max_seq_by_sid(root)  # warms _full_scan_cache + _seq_offsets at file_size
        # Direct call takes the cached-entries branch (full_scan_cache matches
        # file_size) instead of re-reading the file -> line_end via offsets (2436).
        summaries, _ = ing._scan_summaries(path, root, 25)
        assert "ma" in summaries, f"cached-path rebuilt ma summary -- {list(summaries)}"
    finally:
        ing._close_handle_locked(root)


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
    failed = 0
    for test in TESTS:
        print(f"\n--- {test.__name__} ---")
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"{FAIL} {test.__name__}: {exc}")
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            failed += 1
            print(f"{FAIL} {test.__name__} raised")
        else:
            print(f"{PASS} {test.__name__}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} test groups passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
