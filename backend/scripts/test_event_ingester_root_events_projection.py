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
    return EventIngester()


def test_unknown_root_reads() -> None:
    ing = _fresh()
    assert ing.root_events_version("nope") == 0, "version 0 for unknown root"
    assert ing.current_seq("nope") is None, "current_seq None for unknown root"
    assert ing.root_events_by_sid("nope") == {}, "by_sid {} for unknown root"
    assert ing.read_orphan_events("nope") == [], "orphans [] for unknown root"


def test_orphan_projection_build() -> None:
    ing = _fresh()
    root, sid = "rp-build", "sid-A"
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent_data("u1"), source="test")               # orphan (msg_id=None)
    ing.ingest(root, sid="sid-B", event_type="manager_event",
               data=_manager_data("u2"), source="test")             # orphan manager

    proj = ing.root_events_by_sid(root)
    assert set(proj.keys()) == {sid, "sid-B"}, \
        f"projection groups by sid -- keys={sorted(proj.keys())}"
    # agent_message shape: wrapped under {type: agent_message, data: ...}
    a = proj[sid][0]
    assert a["type"] == "agent_message" and a["data"]["uuid"] == "u1", \
        f"agent_message frontend shape -- {a}"
    # manager_event shape: inner event unwrapped
    m = proj["sid-B"][0]
    assert m["type"] == "manager_event" and m["data"]["uuid"] == "u2", \
        f"manager_event frontend shape unwraps inner -- {m}"
    # version + seq advanced from zero
    assert ing.root_events_version(root) > 0, "version > 0 after ingest"
    assert ing.current_seq(root) == 2, \
        f"current_seq == 2 after two ingests -- {ing.current_seq(root)}"


def test_metadata_and_nonrender_excluded() -> None:
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
    assert uids == {"keep"}, f"metadata event excluded from projection -- {uids}"


def test_stamped_suppresses_orphan_rebuild() -> None:
    """In _build_root_events_projection, a stamped (msg_id set) row for a uid
    marks that uid as claimed, so a later orphan with the same uid is dropped."""
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
    assert proj.get(sid, []) == [], \
        f"stamped uid suppresses orphan in rebuild -- {proj.get(sid)}"


def test_ownership_resolved_drops_orphan() -> None:
    """event_ownership_resolved with event_seq pointing at an orphan's seq
    removes that orphan from the rebuilt projection."""
    ing = _fresh()
    root, sid = "rp-owner", "sid-A"
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent_data("ouid"), source="test")             # seq 1
    ing.ingest(root, sid=sid, event_type="event_ownership_resolved",
               data={"event_seq": 1}, source="test")                # resolves seq 1
    proj = ing.root_events_by_sid(root)
    assert proj.get(sid, []) == [], \
        f"ownership_resolved drops resolved orphan -- {proj.get(sid)}"


def test_incremental_cache_update_and_remove() -> None:
    """Drive the incremental maintainers through ingest after the cache is
    primed: orphan append, stamped mutated-same-uid removal, ownership pop."""
    ing = _fresh()
    root, sid = "rp-inc", "sid-A"
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent_data("i1"), source="test")
    ing.root_events_by_sid(root)                                    # prime cache
    cached = ing._root_events_cache.get(root)
    assert cached is not None and sid in cached[1], "cache primed with sid"

    # New orphan uid -> incremental append.
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent_data("i2"), source="test")
    uids = {e["data"]["uuid"] for e in ing._root_events_cache[root][1].get(sid, [])}
    assert {"i1", "i2"} <= uids, f"incremental append adds new orphan uid -- {uids}"

    # Stamped mutated same-uid -> incremental removal of that uid.
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent_data("i2", "changed"), source="test", msg_id="msg-x")
    uids = {e["data"]["uuid"] for e in ing._root_events_cache[root][1].get(sid, [])}
    assert "i2" not in uids, f"stamped ingest removes orphan uid from cache -- {uids}"

    # ownership_resolved -> cache popped entirely (forces next-read rebuild).
    ing.ingest(root, sid=sid, event_type="event_ownership_resolved",
               data={"event_seq": 1}, source="test")
    assert root not in ing._root_events_cache, "ownership ingest pops root cache"


def test_remove_root_event_projection_direct() -> None:
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
    assert "r1" not in remaining_uids and "r2" in remaining_uids, \
        f"remove drops matching uid only -- {remaining_uids}"
    assert "not-a-dict" in projection["sid"], "non-dict entry retained"

    # Removing the last dict leaves the non-dict entry -> sid retained.
    ing._remove_root_event_projection(projection, "sid", uid="r2")
    assert "not-a-dict" in projection["sid"], "sid retained while non-dict remains"
    # No-op on absent sid.
    ing._remove_root_event_projection(projection, "absent", uid="x")
    assert "absent" not in projection, "remove on absent sid is a no-op"

    # Removing the only dict entry with nothing left -> sid is popped.
    solo: dict[str, list[dict]] = {"sid": [{"type": "agent_message", "data": {"uuid": "z"}}]}
    ing._remove_root_event_projection(solo, "sid", uid="z")
    assert "sid" not in solo, f"sid popped when no entries remain -- {solo}"


def test_read_orphan_events_paths() -> None:
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
    assert [e["seq"] for e in full] == [1, 2], \
        f"full orphan scan returns orphans only -- {[e['seq'] for e in full]}"

    # Offset fast path (after_seq=1, _seq_offsets warm from the ingests).
    warm = ing.read_orphan_events(root, after_seq=1)
    assert [e["seq"] for e in warm] == [2], \
        f"offset fast path after_seq=1 -- {[e['seq'] for e in warm]}"

    # Beyond range -> [].
    assert ing.read_orphan_events(root, after_seq=999) == [], "after_seq beyond range -> []"

    # Cold-cache fallback: drop _seq_offsets so after_seq>0 takes the
    # _scan_from branch and still filters to orphans only.
    ing._seq_offsets.pop(root, None)
    cold = ing.read_orphan_events(root, after_seq=0)
    assert [e["seq"] for e in cold] == [1, 2], \
        f"cold-cache fallback scans orphans -- {[e['seq'] for e in cold]}"

    # File missing -> [].
    assert ing.read_orphan_events("missing-root") == [], "missing file -> []"

    # Malformed JSON line, a blank line, and a seq<=after_seq row are all
    # skipped without affecting the orphan result.
    with open(_events_path(root), "a", encoding="utf-8") as fh:
        fh.write("{ this is not json\n\n")
        fh.write(json.dumps({"seq": 0, "sid": "s1", "type": "agent_message",
                             "data": _agent_data("zero"), "source": "x"}) + "\n")
    skipped = ing.read_orphan_events(root)
    assert [e["seq"] for e in skipped] == [1, 2], \
        f"malformed/blank/seq<=after_seq lines skipped -- {[e['seq'] for e in skipped]}"


def test_cached_rows_for_byte_range() -> None:
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
    assert ing.cached_rows_for_byte_range(root, 5, 5) == [], "byte_end<=byte_start -> []"

    # Cache hit: full byte range returns both rows.
    rows = ing.cached_rows_for_byte_range(root, 0, file_size)
    assert rows is not None and len(rows) == 2, f"cache hit returns both rows -- {rows}"

    # Narrow range starting at the second line's offset returns only row 2.
    rows2 = ing.cached_rows_for_byte_range(root, offsets[1], file_size)
    assert rows2 is not None and len(rows2) == 1 \
        and rows2[0]["data"]["uuid"] == "b2", f"narrow range returns row 2 only -- {rows2}"

    # Stale cache (recorded size != file size) -> None.
    ing._full_scan_cache[root] = (0, [])
    assert ing.cached_rows_for_byte_range(root, 0, file_size) is None, \
        "stale cache (size mismatch) -> None"

    # No cache -> None.
    ing._full_scan_cache.pop(root, None)
    assert ing.cached_rows_for_byte_range(root, 0, file_size) is None, "missing cache -> None"

    # Race: path.exists() reports present, then path.stat() raises OSError.
    # Python 3.13 forwards follow_symlinks through Path.exists -> Path.stat,
    # so both must be patched and accept **kwargs; exists=True lets control
    # flow reach the `except OSError: return None` branch around stat().
    ing.root_events_by_sid(root)                                    # rewarm cache
    orig_stat = pathlib.Path.stat
    orig_exists = pathlib.Path.exists
    pathlib.Path.exists = lambda self, **_kw: True  # type: ignore
    pathlib.Path.stat = lambda self, **_kw: (_ for _ in ()).throw(OSError())  # type: ignore
    try:
        assert ing.cached_rows_for_byte_range(root, 0, file_size) is None, \
            "stat OSError -> None"
    finally:
        pathlib.Path.stat = orig_stat  # type: ignore
        pathlib.Path.exists = orig_exists  # type: ignore


def test_would_dedupe_orphan() -> None:
    ing = _fresh()
    root, sid = "rp-wd", "sid-A"
    assert ing.would_dedupe_orphan(root, "agent_message", _agent_data("w1")) is False, \
        "unseen uid:hash not a dedup"
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=_agent_data("w1", "original"), source="test")
    # Same uid + same data -> would be deduped.
    assert ing.would_dedupe_orphan(root, "agent_message", _agent_data("w1", "original")) is True, \
        "seen uid:hash is a dedup"
    # Same uid, MUTATED data -> NOT a dedup (the streaming-update rule).
    assert ing.would_dedupe_orphan(root, "agent_message", _agent_data("w1", "mutated")) is False, \
        "same uid mutated data is not a dedup"


def test_cold_root_events_by_sid() -> None:
    """First-touch root_events_by_sid on a fresh ingester with only on-disk
    state: lazily scans, rebuilds the projection via the full-scan cache hit
    fast path, and returns {} when no projection-affecting rows exist."""
    # File with only non-projection rows -> version 0 -> empty projection.
    root_empty, sid = "rp-cold-empty", "sid-A"
    _append_raw(root_empty, {"seq": 1, "ts": "t", "sid": sid, "type": "worker_event",
                             "data": {"event": {"type": "worker_event"}}, "source": "x"})
    ing = _fresh()
    proj_empty = ing.root_events_by_sid(root_empty)
    assert proj_empty == {}, f"cold rebuild with no projection rows -> {{}} -- {proj_empty}"

    # File with a real projection row -> rebuilt and returned.
    root_full = "rp-cold-full"
    _append_raw(root_full, {"seq": 1, "ts": "t", "sid": sid, "type": "agent_message",
                            "data": _agent_data("cold1"), "source": "x"})
    ing2 = _fresh()
    proj_full = ing2.root_events_by_sid(root_full)
    uids = {e["data"]["uuid"] for e in proj_full.get(sid, [])}
    assert uids == {"cold1"}, f"cold rebuild returns projection row -- {uids}"


def test_build_filters_mixed_raw_rows() -> None:
    """_build_root_events_projection skips non-str sid, non-render types,
    and non-int event_seq; a stamped no-uid row claims nothing; an orphan
    with no uid is still rendered."""
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
    assert len(rendered) == 1, f"only the no-uid orphan is rendered -- {len(rendered)}"
    assert "uuid" not in rendered[0].get("data", {}), \
        f"rendered row carries no uuid -- {rendered[0]}"


def test_cold_scan_max_seq_version() -> None:
    """On first touch of a root whose events.jsonl already exists on disk
    (e.g. written by a previous process), the version read lazily runs
    _scan_max_seq: version becomes the count of projection-affecting rows
    and current_seq the parsed-line count."""
    root, sid = "rp-cold", "sid-A"
    for i in range(1, 4):
        _append_raw(root, {"seq": i, "ts": "t", "sid": sid, "type": "agent_message",
                           "data": _agent_data(f"c{i}"), "source": "x"})
    ing = _fresh()                                                  # no in-memory state yet
    assert ing.root_events_version(root) == 3, \
        f"cold scan sets version to projection-row count -- {ing.root_events_version(root)}"
    assert ing.current_seq(root) == 3, \
        f"cold scan sets current_seq to parsed-line count -- {ing.current_seq(root)}"


def test_extend_full_scan_path() -> None:
    """_extend_full_scan: a partial full-scan cache (recorded size < file
    size) is extended by parsing appended bytes. Driven through
    _read_all_events_locked (which owns the cached[0] < file_size branch)
    under the per-root lock it requires."""
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
    assert new_size > recorded_size, "external append grew the file"

    # _read_all_events_locked must be called with the root lock held; its
    # cached[0] < file_size branch routes through _extend_full_scan.
    lock = ing._locks.setdefault(root, threading.Lock())
    with lock:
        rows = ing._read_all_events_locked(path, root, new_size)
    uids = {ing._extract_uuid(r.get("data") or {}) for r in rows}
    assert {"e1", "e2"} <= uids, f"extend-full-scan picks up externally appended row -- {uids}"
    assert ing._full_scan_cache[root][0] == new_size, \
        f"cache high-water advanced to new size -- {ing._full_scan_cache[root][0]}!={new_size}"


def test_stamped_no_uid_bumps_cache() -> None:
    """A stamped (msg_id set) event whose data carries NO uuid takes the
    no-op-removal branch of _update_root_events_cache_for_entry: it just
    re-stamps the cache version without mutating the projection."""
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
    assert cached[1] == proj_before, "no-uid stamped leaves projection untouched"
    assert cached[0] >= version_before, "no-uid stamped re-stamps cache version"


def test_concurrent_extend_full_scan_is_threadsafe() -> None:
    """_extend_full_scan releases the root lock around its file read; many
    concurrent readers must not corrupt the shared cache. Smoke test only."""
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
    assert not errors, f"concurrent reads raised no errors -- {errors[:1]}"


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
    failures: list[str] = []
    for test in TESTS:
        try:
            test()
            print(f"{PASS} {test.__name__}")
        except Exception as exc:  # noqa: BLE001 - report any failure
            print(f"{FAIL} {test.__name__}: {exc!r}")
            failures.append(test.__name__)
    print(f"\n{len(TESTS) - len(failures)}/{len(TESTS)} test groups passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
