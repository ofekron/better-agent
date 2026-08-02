"""Unit coverage for the root_events projection + orphan/dedup read cluster
of event_ingester.py: root_events_by_sid / root_events_version / current_seq,
the projection builders/maintainers (_build_root_events_projection,
_root_event_frontend_shape, _update_root_events_cache_for_entry,
_remove_root_event_projection), read_orphan_events, cached_rows_for_byte_range,
would_dedupe_orphan, and _extend_full_scan.

Run with:
    cd backend && .venv/bin/python scripts/test_event_ingester_root_events_projection.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import threading

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ingester-rootproj-")

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


def _agent_data(uid: str, text: str = "hi") -> dict:
    return {
        "uuid": uid,
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _manager_data(uid: str) -> dict:
    # manager_event rows carry the inner render event under data.event;
    # _extract_uuid unwraps event.data.uuid, frontend shape unwraps event.
    return {
        "event": {
            "type": "manager_event",
            "data": {"uuid": uid, "type": "assistant",
                     "message": {"content": [{"type": "text", "text": "mgr"}]}},
        }
    }


def _events_path(root_id: str) -> str:
    return os.path.join(_TMP_HOME, "sessions", root_id, "events.jsonl")


def _append_raw(root_id: str, entry: dict) -> None:
    """Append a hand-built row directly, bypassing ingest — used to plant
    rows ingest's own validation would refuse (non-str sid, malformed)."""
    path = _events_path(root_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _fresh() -> EventIngester:
    ing = EventIngester()
    return ing


def test_unknown_root_reads() -> bool:
    ok = True
    ing = _fresh()
    ok = _check(ing.root_events_version("nope") == 0, "version 0 for unknown root") and ok
    ok = _check(ing.current_seq("nope") is None, "current_seq None for unknown root") and ok
    ok = _check(ing.root_events_by_sid("nope") == {}, "by_sid {} for unknown root") and ok
    ok = _check(ing.read_orphan_events("nope") == [], "orphans [] for unknown root") and ok
    return ok


def test_orphan_projection_build() -> bool:
    ok = True
    ing = _fresh()
    root, sid = "rp-build", "sid-A"
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent_data("u1"), source="test")               # orphan (msg_id=None)
    ing.ingest(root, sid="sid-B", event_type="manager_event",
               data=_manager_data("u2"), source="test")             # orphan manager

    proj = ing.root_events_by_sid(root)
    ok = _check(set(proj.keys()) == {sid, "sid-B"}, "projection groups by sid",
                f"keys={sorted(proj.keys())}") and ok
    # agent_message shape: wrapped under {type: agent_message, data: ...}
    a = proj[sid][0]
    ok = _check(a["type"] == "agent_message" and a["data"]["uuid"] == "u1",
                "agent_message frontend shape", f"{a}") and ok
    # manager_event shape: inner event unwrapped
    m = proj["sid-B"][0]
    ok = _check(m["type"] == "manager_event" and m["data"]["uuid"] == "u2",
                "manager_event frontend shape unwraps inner", f"{m}") and ok
    # version + seq advanced from zero
    ok = _check(ing.root_events_version(root) > 0, "version > 0 after ingest") and ok
    ok = _check(ing.current_seq(root) == 2, "current_seq == 2 after two ingests",
                f"{ing.current_seq(root)}") and ok
    return ok


def test_metadata_and_nonrender_excluded() -> bool:
    ok = True
    ing = _fresh()
    root, sid = "rp-meta", "sid-A"
    ing.ingest(root, sid=sid, event_type="agent_message",
               data={"uuid": "keep", "type": "assistant",
                     "message": {"content": [{"type": "text", "text": "x"}]}},
               source="test")
    # metadata data-type (ai-title) — excluded by is_metadata_event
    ing.ingest(root, sid=sid, event_type="agent_message",
               data={"uuid": "drop", "type": "ai-title", "title": "t"},
               source="test")
    proj = ing.root_events_by_sid(root)
    uids = {e["data"]["uuid"] for e in proj.get(sid, [])}
    ok = _check(uids == {"keep"}, "metadata event excluded from projection",
                f"{uids}") and ok
    return ok


def test_stamped_suppresses_orphan_rebuild() -> bool:
    """In _build_root_events_projection, a stamped (msg_id set) row for a uid
    marks that uid as claimed, so a later orphan with the same uid is dropped."""
    ok = True
    ing = _fresh()
    root, sid = "rp-stamp", "sid-A"
    # Stamped row first (msg_id set), different text so it is not deduped.
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent_data("duid", "stamped"), source="test", msg_id="msg-1")
    # Orphan with SAME uid but MUTATED data — not a hash duplicate, so it is
    # written, yet the rebuild must still drop it because the uid is stamped.
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent_data("duid", "orphan"), source="test")
    proj = ing.root_events_by_sid(root)
    ok = _check(proj.get(sid, []) == [], "stamped uid suppresses orphan in rebuild",
                f"{proj.get(sid)}") and ok
    return ok


def test_ownership_resolved_drops_orphan() -> bool:
    """event_ownership_resolved with event_seq pointing at an orphan's seq
    removes that orphan from the rebuilt projection."""
    ok = True
    ing = _fresh()
    root, sid = "rp-owner", "sid-A"
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent_data("ouid"), source="test")             # seq 1
    ing.ingest(root, sid=sid, event_type="event_ownership_resolved",
               data={"event_seq": 1}, source="test")                # resolves seq 1
    proj = ing.root_events_by_sid(root)
    ok = _check(proj.get(sid, []) == [], "ownership_resolved drops resolved orphan",
                f"{proj.get(sid)}") and ok
    return ok


def test_incremental_cache_update_and_remove() -> bool:
    """Drive the incremental maintainers through ingest after the cache is
    primed: orphan append, stamped mutated-same-uid removal, ownership pop."""
    ok = True
    ing = _fresh()
    root, sid = "rp-inc", "sid-A"
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent_data("i1"), source="test")
    ing.root_events_by_sid(root)                                    # prime cache
    cached = ing._root_events_cache.get(root)
    ok = _check(cached is not None and sid in cached[1], "cache primed with sid") and ok

    # New orphan uid -> incremental append.
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent_data("i2"), source="test")
    uids = {e["data"]["uuid"] for e in ing._root_events_cache[root][1].get(sid, [])}
    ok = _check({"i1", "i2"} <= uids, "incremental append adds new orphan uid",
                f"{uids}") and ok

    # Stamped mutated same-uid -> incremental removal of that uid.
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent_data("i2", "changed"), source="test", msg_id="msg-x")
    uids = {e["data"]["uuid"] for e in ing._root_events_cache[root][1].get(sid, [])}
    ok = _check("i2" not in uids, "stamped ingest removes orphan uid from cache",
                f"{uids}") and ok

    # ownership_resolved -> cache popped entirely (forces next-read rebuild).
    ing.ingest(root, sid=sid, event_type="event_ownership_resolved",
               data={"event_seq": 1}, source="test")
    ok = _check(root not in ing._root_events_cache, "ownership ingest pops root cache") and ok
    return ok


def test_remove_root_event_projection_direct() -> bool:
    ok = True
    ing = _fresh()
    projection: dict[str, list[dict]] = {
        "sid": [
            {"type": "agent_message", "data": {"uuid": "r1"}},
            {"type": "agent_message", "data": {"uuid": "r2"}},
            "not-a-dict",  # non-dict entries are retained untouched
        ]
    }
    ing._remove_root_event_projection(projection, "sid", uid="r1")
    remaining_uids = [e.get("data", {}).get("uuid") for e in projection["sid"]
                      if isinstance(e, dict)]
    ok = _check("r1" not in remaining_uids and "r2" in remaining_uids,
                "remove drops matching uid only", f"{remaining_uids}") and ok
    ok = _check("not-a-dict" in projection["sid"], "non-dict entry retained") and ok

    # Removing the last dict leaves the non-dict entry -> sid retained.
    ing._remove_root_event_projection(projection, "sid", uid="r2")
    ok = _check("not-a-dict" in projection["sid"], "sid retained while non-dict remains") and ok
    # No-op on absent sid.
    ing._remove_root_event_projection(projection, "absent", uid="x")
    ok = _check("absent" not in projection, "remove on absent sid is a no-op") and ok

    # Removing the only dict entry with nothing left -> sid is popped.
    solo: dict[str, list[dict]] = {"sid": [{"type": "agent_message", "data": {"uuid": "z"}}]}
    ing._remove_root_event_projection(solo, "sid", uid="z")
    ok = _check("sid" not in solo, "sid popped when no entries remain", f"{solo}") and ok
    return ok


def test_read_orphan_events_paths() -> bool:
    ok = True
    ing = _fresh()
    root = "rp-orphan"
    # Two orphan rows (msg_id=None) + one stamped row (msg_id set, filtered out).
    ing.ingest(root, sid="s1", event_type="agent_message",
               data=_agent_data("o1"), source="test")               # seq 1
    ing.ingest(root, sid="s1", event_type="agent_message",
               data=_agent_data("o2"), source="test")               # seq 2
    ing.ingest(root, sid="s1", event_type="agent_message",
               data=_agent_data("s3"), source="test", msg_id="m")   # seq 3, stamped

    # Full scan (after_seq=0) returns only the two orphans, in order.
    full = ing.read_orphan_events(root)
    ok = _check([e["seq"] for e in full] == [1, 2], "full orphan scan returns orphans only",
                f"{[e['seq'] for e in full]}") and ok

    # Offset fast path (after_seq=1, _seq_offsets warm from the ingests).
    warm = ing.read_orphan_events(root, after_seq=1)
    ok = _check([e["seq"] for e in warm] == [2], "offset fast path after_seq=1",
                f"{[e['seq'] for e in warm]}") and ok

    # Beyond range -> [].
    ok = _check(ing.read_orphan_events(root, after_seq=999) == [],
                "after_seq beyond range -> []") and ok

    # Cold-cache fallback: drop _seq_offsets so after_seq>0 takes the
    # _scan_from branch and still filters to orphans only.
    ing._seq_offsets.pop(root, None)
    cold = ing.read_orphan_events(root, after_seq=0)
    ok = _check([e["seq"] for e in cold] == [1, 2], "cold-cache fallback scans orphans",
                f"{[e['seq'] for e in cold]}") and ok

    # File missing -> [].
    ok = _check(ing.read_orphan_events("missing-root") == [], "missing file -> []") and ok

    # Malformed JSON line, a blank line, and a seq<=after_seq row are all
    # skipped without affecting the orphan result.
    with open(_events_path(root), "a", encoding="utf-8") as fh:
        fh.write("{ this is not json\n\n")
        fh.write(json.dumps({"seq": 0, "sid": "s1", "type": "agent_message",
                             "data": _agent_data("zero"), "source": "x"}) + "\n")
    skipped = ing.read_orphan_events(root)
    ok = _check([e["seq"] for e in skipped] == [1, 2],
                "malformed/blank/seq<=after_seq lines skipped",
                f"{[e['seq'] for e in skipped]}") and ok
    return ok


def test_cached_rows_for_byte_range() -> bool:
    ok = True
    ing = _fresh()
    root = "rp-byterange"
    ing.ingest(root, sid="s", event_type="agent_message",
               data=_agent_data("b1"), source="test")
    ing.ingest(root, sid="s", event_type="agent_message",
               data=_agent_data("b2"), source="test")
    ing.root_events_by_sid(root)                                    # warm full-scan cache
    offsets = ing._seq_offsets[root]
    file_size = ing._full_scan_cache[root][0]

    # byte_end <= byte_start -> [] (path exists).
    ok = _check(ing.cached_rows_for_byte_range(root, 5, 5) == [],
                "byte_end<=byte_start -> []") and ok

    # Cache hit: full byte range returns both rows.
    rows = ing.cached_rows_for_byte_range(root, 0, file_size)
    ok = _check(rows is not None and len(rows) == 2, "cache hit returns both rows",
                f"{rows}") and ok

    # Narrow range starting at the second line's offset returns only row 2.
    rows2 = ing.cached_rows_for_byte_range(root, offsets[1], file_size)
    ok = _check(rows2 is not None and len(rows2) == 1
                and rows2[0]["data"]["uuid"] == "b2", "narrow range returns row 2 only",
                f"{rows2}") and ok

    # Stale cache (recorded size != file size) -> None.
    ing._full_scan_cache[root] = (0, [])
    ok = _check(ing.cached_rows_for_byte_range(root, 0, file_size) is None,
                "stale cache (size mismatch) -> None") and ok

    # No cache -> None.
    ing._full_scan_cache.pop(root, None)
    ok = _check(ing.cached_rows_for_byte_range(root, 0, file_size) is None,
                "missing cache -> None") and ok

    # path.stat() OSError -> None.
    ing.root_events_by_sid(root)                                    # rewarm cache
    orig = pathlib.Path.stat
    pathlib.Path.stat = lambda self: (_ for _ in ()).throw(OSError())  # type: ignore
    try:
        ok = _check(ing.cached_rows_for_byte_range(root, 0, file_size) is None,
                    "stat OSError -> None") and ok
    finally:
        pathlib.Path.stat = orig  # type: ignore
    return ok


def test_would_dedupe_orphan() -> bool:
    ok = True
    ing = _fresh()
    root, sid = "rp-wd", "sid-A"
    ok = _check(ing.would_dedupe_orphan(root, "agent_message", _agent_data("w1")) is False,
                "unseen uid:hash not a dedup") and ok
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent_data("w1", "original"), source="test")
    # Same uid + same data -> would be deduped.
    ok = _check(ing.would_dedupe_orphan(root, "agent_message", _agent_data("w1", "original")) is True,
                "seen uid:hash is a dedup") and ok
    # Same uid, MUTATED data -> NOT a dedup (the streaming-update rule).
    ok = _check(ing.would_dedupe_orphan(root, "agent_message", _agent_data("w1", "mutated")) is False,
                "same uid mutated data is not a dedup") and ok
    return ok


def test_cold_root_events_by_sid() -> bool:
    """First-touch root_events_by_sid on a fresh ingester with only on-disk
    state: lazily scans, rebuilds the projection via the full-scan cache hit
    fast path, and returns {} when no projection-affecting rows exist."""
    ok = True
    # File with only non-projection rows -> version 0 -> empty projection.
    root_empty, sid = "rp-cold-empty", "sid-A"
    _append_raw(root_empty, {"seq": 1, "ts": "t", "sid": sid, "type": "worker_event",
                             "data": {"event": {"type": "worker_event"}}, "source": "x"})
    ing = _fresh()
    proj_empty = ing.root_events_by_sid(root_empty)
    ok = _check(proj_empty == {}, "cold rebuild with no projection rows -> {}",
                f"{proj_empty}") and ok

    # File with a real projection row -> rebuilt and returned.
    root_full = "rp-cold-full"
    _append_raw(root_full, {"seq": 1, "ts": "t", "sid": sid, "type": "agent_message",
                            "data": _agent_data("cold1"), "source": "x"})
    ing2 = _fresh()
    proj_full = ing2.root_events_by_sid(root_full)
    uids = {e["data"]["uuid"] for e in proj_full.get(sid, [])}
    ok = _check(uids == {"cold1"}, "cold rebuild returns projection row", f"{uids}") and ok
    return ok


def test_build_filters_mixed_raw_rows() -> bool:
    """_build_root_events_projection skips non-str sid, non-render types,
    and non-int event_seq; a stamped no-uid row claims nothing; an orphan
    with no uid is still rendered."""
    ok = True
    root, sid = "rp-mixed", "sid-A"
    _append_raw(root, {"seq": 1, "ts": "t", "sid": 123, "type": "agent_message",  # non-str sid
                       "data": _agent_data("m1"), "source": "x"})
    _append_raw(root, {"seq": 2, "ts": "t", "sid": sid, "type": "worker_event",    # non-render type
                       "data": {"event": {"type": "worker_event"}}, "source": "x"})
    _append_raw(root, {"seq": 3, "ts": "t", "sid": sid,                            # non-int event_seq
                       "type": "event_ownership_resolved", "data": {"event_seq": "nope"},
                       "source": "x"})
    _append_raw(root, {"seq": 4, "ts": "t", "sid": sid, "type": "agent_message",   # stamped, no uid
                       "data": {"type": "assistant", "message": {"content": []}},
                       "source": "x", "msg_id": "m"})
    _append_raw(root, {"seq": 5, "ts": "t", "sid": sid, "type": "agent_message",   # orphan, no uid
                       "data": {"type": "assistant", "message": {"content": [{"type": "text", "text": "z"}]}},
                       "source": "x"})
    ing = _fresh()
    proj = ing.root_events_by_sid(root)
    rendered = proj.get(sid, [])
    ok = _check(len(rendered) == 1, "only the no-uid orphan is rendered",
                f"{len(rendered)}") and ok
    ok = _check("uuid" not in rendered[0].get("data", {}), "rendered row carries no uuid",
                f"{rendered[0]}") and ok
    return ok


def test_cold_scan_max_seq_version() -> bool:
    """On first touch of a root whose events.jsonl already exists on disk
    (e.g. written by a previous process), the version read lazily runs
    _scan_max_seq: version becomes the count of projection-affecting rows
    and current_seq the parsed-line count."""
    ok = True
    root, sid = "rp-cold", "sid-A"
    for i in range(1, 4):
        _append_raw(root, {"seq": i, "ts": "t", "sid": sid, "type": "agent_message",
                           "data": _agent_data(f"c{i}"), "source": "x"})
    ing = _fresh()                                                  # no in-memory state yet
    ok = _check(ing.root_events_version(root) == 3, "cold scan sets version to projection-row count",
                f"{ing.root_events_version(root)}") and ok
    ok = _check(ing.current_seq(root) == 3, "cold scan sets current_seq to parsed-line count",
                f"{ing.current_seq(root)}") and ok
    return ok


def test_extend_full_scan_path() -> bool:
    """_extend_full_scan: a partial full-scan cache (recorded size < file
    size) is extended by parsing appended bytes. Driven through
    _read_all_events_locked (which owns the cached[0] < file_size branch)
    under the per-root lock it requires."""
    ok = True
    ing = _fresh()
    root, sid = "rp-extend", "sid-A"
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent_data("e1"), source="test")
    ing.root_events_by_sid(root)                                    # warm full-scan cache
    recorded_size = ing._full_scan_cache[root][0]

    # Simulate an external append past the cached high-water, including a
    # malformed line and a blank line that the extend parse must skip.
    with open(_events_path(root), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "seq": 2, "ts": "1970-01-01T00:00:00+00:00", "sid": sid,
            "type": "agent_message", "data": _agent_data("e2"), "source": "external",
        }) + "\n")
        fh.write("{ broken json\n\n")
    path = pathlib.Path(_events_path(root))
    new_size = path.stat().st_size
    ok = _check(new_size > recorded_size, "external append grew the file") and ok

    # _read_all_events_locked must be called with the root lock held; its
    # cached[0] < file_size branch routes through _extend_full_scan.
    lock = ing._locks.setdefault(root, threading.Lock())
    with lock:
        rows = ing._read_all_events_locked(path, root, new_size)
    uids = {ing._extract_uuid(r.get("data") or {}) for r in rows}
    ok = _check({"e1", "e2"} <= uids, "extend-full-scan picks up externally appended row",
                f"{uids}") and ok
    ok = _check(ing._full_scan_cache[root][0] == new_size, "cache high-water advanced to new size",
                f"{ing._full_scan_cache[root][0]}!={new_size}") and ok
    return ok


def test_stamped_no_uid_bumps_cache() -> None:
    """A stamped (msg_id set) event whose data carries NO uuid takes the
    no-op-removal branch of _update_root_events_cache_for_entry: it just
    re-stamps the cache version without mutating the projection."""
    ok = True
    ing = _fresh()
    root, sid = "rp-nouid", "sid-A"
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent_data("n1"), source="test")
    ing.root_events_by_sid(root)                                    # prime cache
    version_before = ing._root_events_cache[root][0]
    proj_before = ing._root_events_cache[root][1]

    ing.ingest(root, sid=sid, event_type="agent_message",
               data={"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}},
               source="test", msg_id="msg-nouid")                   # stamped, no uuid

    cached = ing._root_events_cache[root]
    ok = _check(cached[1] == proj_before, "no-uid stamped leaves projection untouched") and ok
    ok = _check(cached[0] >= version_before, "no-uid stamped re-stamps cache version") and ok
    return ok


def test_concurrent_extend_full_scan_is_threadsafe() -> bool:
    """_extend_full_scan releases the root lock around its file read; many
    concurrent readers must not corrupt the shared cache. Smoke test only."""
    ok = True
    ing = _fresh()
    root, sid = "rp-conc", "sid-A"
    for i in range(20):
        ing.ingest(root, sid=sid, event_type="agent_message",
                   data=_agent_data(f"c{i}"), source="test")

    errors: list[Exception] = []

    def _reader() -> None:
        try:
            for _ in range(50):
                ing.root_events_by_sid(root)
        except Exception as exc:  # noqa: BLE001 - record any failure
            errors.append(exc)

    threads = [threading.Thread(target=_reader) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok = _check(not errors, "concurrent reads raised no errors", f"{errors[:1]}") and ok
    return ok


TESTS = [
    test_unknown_root_reads,
    test_orphan_projection_build,
    test_metadata_and_nonrender_excluded,
    test_stamped_suppresses_orphan_rebuild,
    test_ownership_resolved_drops_orphan,
    test_incremental_cache_update_and_remove,
    test_remove_root_event_projection_direct,
    test_read_orphan_events_paths,
    test_cached_rows_for_byte_range,
    test_would_dedupe_orphan,
    test_extend_full_scan_path,
    test_concurrent_extend_full_scan_is_threadsafe,
    test_cold_scan_max_seq_version,
    test_stamped_no_uid_bumps_cache,
    test_cold_root_events_by_sid,
    test_build_filters_mixed_raw_rows,
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
