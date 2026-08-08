"""Unit coverage for the bootstrap / sidecar / torn-tail cluster of
event_ingester.py:

  - _ref_ctx_for_root: cwd-hit, empty-cwd, non-primary node, lookup-failure
  - _event_file_signature: stat OSError
  - _seed_write_caches_locked: sort_keys hash fallback, non-str sid /
    non-int seq skip, render projection, root-event candidate +
    ownership-resolved resolution folding
  - _load_event_meta_sidecar_locked: missing signature, valid load with
    root_events_by_sid projection, stale-signature rejection
  - _write_event_meta_sidecar_locked: missing signature, OSError
    tmp-unlink fallback (success + nested failure)
  - _load_event_summaries_sidecar_locked: missing signature, non-int
    resolution key skip, valid load
  - _valid_worker_rows / _valid_seq_offsets: every rejection branch +
    accept
  - _write_event_summaries_sidecar_locked: missing signature, guard
    mismatch skip, OSError tmp-unlink fallback
  - _close_handle_locked: fsync OSError on drain
  - _prune_append_handles: victim_lock-None skip + victim_id-None return
  - _fsync_dirty_now: fh-None skip + fsync OSError
  - _ensure_open: torn-tail recovery (blank line reset + partial trailing
    JSON truncation)

Run with:
    cd backend && .venv/bin/python scripts/test_event_ingester_bootstrap_cluster.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ingester-boot-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import event_ingester  # noqa: E402
import session_store  # noqa: E402
from event_ingester import EventIngester  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _fresh() -> EventIngester:
    return EventIngester()


def _root_dir(root_id: str) -> str:
    return os.path.join(_TMP_HOME, "sessions", root_id)


def _events_path(root_id: str) -> str:
    return os.path.join(_root_dir(root_id), "events.jsonl")


def _meta_path(root_id: str) -> str:
    return os.path.join(_root_dir(root_id), "event_meta.json")


def _summaries_path(root_id: str) -> str:
    return os.path.join(_root_dir(root_id), "event_summaries.json")


def _write_bytes(root_id: str, name: str, data: bytes) -> str:
    path = os.path.join(_root_dir(root_id), name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


# --------------------------------------------------------------------------- #
# _ref_ctx_for_root
# --------------------------------------------------------------------------- #
def test_ref_ctx_for_root():
    # Primary node, real cwd -> (cwd, False).
    sess = session_store.create_session(
        name="boot", cwd="/tmp", node_id="primary", id="boot-refctx-primary",
    )
    cwd, is_remote = event_ingester._ref_ctx_for_root(sess["id"])
    assert cwd == "/tmp" and is_remote is False, f"primary session -> (cwd, False) -- {cwd!r},{is_remote!r}"
    # Empty cwd -> (None, False). create_session fills empty cwd with
    # os.getcwd(), so drive this branch by injecting a summary whose cwd
    # is the empty string directly.
    orig_sfm = session_store.summary_fields_many

    def _empty_cwd(sids, fields):  # noqa: ANN001
        return {sid: {"cwd": "", "node_id": "primary"} for sid in sids}

    session_store.summary_fields_many = _empty_cwd  # type: ignore[assignment]
    try:
        cwd2, is_remote2 = event_ingester._ref_ctx_for_root("boot-refctx-nocwd")
    finally:
        session_store.summary_fields_many = orig_sfm  # type: ignore[assignment]
    assert cwd2 is None and is_remote2 is False, f"empty cwd -> (None, False) -- {cwd2!r},{is_remote2!r}"
    # Non-primary node -> assume_exists True (files live on the node).
    sess3 = session_store.create_session(
        name="boot3", cwd="/tmp", node_id="node-remote", id="boot-refctx-remote",
    )
    cwd3, is_remote3 = event_ingester._ref_ctx_for_root(sess3["id"])
    assert cwd3 == "/tmp" and is_remote3 is True, f"non-primary node -> (cwd, True) -- {cwd3!r},{is_remote3!r}"
    # Lookup raises -> defensive (None, False).
    orig = session_store.summary_fields_many

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    session_store.summary_fields_many = _boom  # type: ignore[assignment]
    try:
        cwd4, is_remote4 = event_ingester._ref_ctx_for_root("never")
    finally:
        session_store.summary_fields_many = orig  # type: ignore[assignment]
    assert cwd4 is None and is_remote4 is False, f"lookup exception -> (None, False) -- {cwd4!r},{is_remote4!r}"
    # Unknown root (no summary) -> (None, False) via the dict-miss path.
    cwd5, is_remote5 = event_ingester._ref_ctx_for_root("does-not-exist-sid")
    assert cwd5 is None and is_remote5 is False, f"unknown root -> (None, False) -- {cwd5!r},{is_remote5!r}"


# --------------------------------------------------------------------------- #
# _event_file_signature: stat OSError
# --------------------------------------------------------------------------- #
def test_event_file_signature_oserror():
    # A path whose parent does not exist -> stat raises OSError -> None.
    missing = Path(_root_dir("sig-missing")) / "events.jsonl"
    assert EventIngester._event_file_signature(missing) is None, f"stat OSError -> None -- {missing}"
    assert EventIngester._event_file_identity(missing) is None, f"identity stat OSError -> None -- {missing}"
    # Existing file -> real (mtime_ns, size) tuple.
    real = Path(_write_bytes("sig-ok", "events.jsonl", b"hi\n"))
    sig = EventIngester._event_file_signature(real)
    assert sig is not None and sig[1] == 3, f"existing file signature -- {sig}"


# --------------------------------------------------------------------------- #
# _seed_write_caches_locked: hash fallback, sid/seq skip, projection folding
# --------------------------------------------------------------------------- #
def test_seed_write_caches_locked():
    ing = _fresh()
    root = "boot-seed"
    # Mixed-type dict keys (int + str) -> json.dumps(sort_keys=True) raises
    # TypeError -> hash fallback (204-205). The entry also carries a valid
    # sid/seq so the projection branches run.
    entries = [
        # 1) mixed-key data, normal render/root-projection agent_message.
        {"seq": 1, "sid": "s1", "type": "agent_message",
         "data": {1: "a", "uuid": "u1"}, "msg_id": "m1"},
        # 2) non-str sid -> projection skip (214).
        {"seq": 2, "sid": 123, "type": "agent_message", "data": {"uuid": "u2"}},
        # 3) non-int seq -> projection skip (214).
        {"seq": "x", "sid": "s1", "type": "agent_message", "data": {"uuid": "u3"}},
        # 4) manager_event with NO msg_id -> root-event candidate (223-224).
        {"seq": 3, "sid": "s1", "type": "manager_event",
         "data": {"uuid": "u4"}},
        # 5) event_ownership_resolved -> resolved seq folds out candidate (225-228).
        {"seq": 4, "sid": "s1", "type": "event_ownership_resolved",
         "data": {"event_seq": 3}},
        # 6) ownership_resolved with NON-int event_seq -> 227 False branch
        #    (event_seq not added to resolved set).
        {"seq": 6, "sid": "s1", "type": "event_ownership_resolved",
         "data": {"event_seq": "not-int"}},
        # 7) non-projection type -> _affects_root_events_projection False (221).
        {"seq": 7, "sid": "s1", "type": "user_message", "data": {"uuid": "u7"}},
    ]
    seq_offsets = [0, 10, 20, 30, 40, 50, 60]
    ing._seed_write_caches_locked(root, entries, seq_offsets, 70, (1, 2, 3, 70))
    # seq seeded to count of entries.
    assert ing._seq[root] == 7, f"seq seeded to entry count -- {ing._seq[root]}"
    assert ing._seq_offsets[root] == seq_offsets, "seq_offsets seeded"
    # max_seq_by_sid only captures valid sid/seq (entries 2,3 skipped).
    assert ing._max_seq_by_sid[root] == {"s1": 7}, f"max_seq_by_sid s1==7 -- {ing._max_seq_by_sid[root]}"
    # render projection skips user_message but INCLUDES ownership_resolved
    # (it is in the render set), so s1 max = max(seq 1, 3, 4, 6) = 6.
    assert ing._render_seq_by_sid[root] == {"s1": 6}, (
        f"render_seq s1==6 (agent+manager+ownership) -- {ing._render_seq_by_sid[root]}")
    # candidate version: seq 3 was a candidate, then resolved -> folds to 0.
    assert ing._root_events_candidate_version[root] == 0, (
        f"resolved candidate folds to 0 -- {ing._root_events_candidate_version[root]}")
    # root_events_version counts every root-projection row (agent/manager/
    # ownership_resolved): entries 1,4,5,6 = 4 (2/3 skipped, 7 non-projection).
    assert ing._root_events_version[root] == 4, f"root_events_version == 4 -- {ing._root_events_version[root]}"
    # mixed-key fallback hash present in seen set (uid-keyed).
    seen = ing._seen_uuids[root]
    assert any(k.startswith("u1:") for k in seen), f"mixed-key data hashed via fallback -- {seen}"


# --------------------------------------------------------------------------- #
# _load_event_meta_sidecar_locked
# --------------------------------------------------------------------------- #
def test_load_event_meta_sidecar():
    ing = _fresh()
    root = "boot-meta"
    # Missing events file -> signature None -> None (253).
    ret = ing._load_event_meta_sidecar_locked(root, Path(_events_path(root)))
    assert ret is None, f"missing signature -> None -- {ret}"

    # Write events.jsonl + a fresh sidecar whose signature matches.
    epath = _write_bytes(root, "events.jsonl", b'{"seq":1}\n{"seq":2}\n')
    sig = EventIngester._event_file_signature(Path(epath))
    assert sig is not None
    sidecar = {
        "mtime_ns": sig[0], "size": sig[1], "seq": 2,
        "max_seq_by_sid": {"s1": 2}, "render_seq_by_sid": {"s1": 2},
        "root_events_version": 5, "root_events_candidate_version": 1,
        "root_events_by_sid": {"s1": [{"seq": 1}, {"seq": 2}]},
    }
    _write_bytes(root, "event_meta.json", json.dumps(sidecar).encode())
    ret = ing._load_event_meta_sidecar_locked(root, Path(epath))
    assert ret == {"s1": 2}, f"valid load returns max_by_sid -- {ret}"
    assert ing._seq[root] == 2, f"seq loaded from sidecar -- {ing._seq.get(root)}"
    assert ing._root_events_version[root] == 5, f"root_events_version loaded -- {ing._root_events_version[root]}"
    # root_events_cache populated (282-291).
    cached = ing._root_events_cache.get(root)
    assert cached is not None and cached[0] == 5 and "s1" in cached[1], f"root_events_cache populated -- {cached}"

    # Stale signature -> rejected.
    ing2 = _fresh()
    sidecar["mtime_ns"] = sidecar["mtime_ns"] + 9999
    _write_bytes(root, "event_meta.json", json.dumps(sidecar).encode())
    ret2 = ing2._load_event_meta_sidecar_locked(root, Path(epath))
    assert ret2 is None, f"stale signature -> None -- {ret2}"

    # Non-dict root_events_by_sid -> projection skipped (282 False branch).
    ing3 = _fresh()
    sidecar2 = dict(sidecar)
    sidecar2["mtime_ns"] = sig[0]
    sidecar2["root_events_by_sid"] = "not-a-dict"
    _write_bytes(root, "event_meta.json", json.dumps(sidecar2).encode())
    ret3 = ing3._load_event_meta_sidecar_locked(root, Path(epath))
    assert ret3 == {"s1": 2}, f"non-dict root_events still loads max_by_sid -- {ret3}"
    assert ing3._root_events_cache.get(root) is None, (
        f"non-dict root_events -> no cache entry -- {ing3._root_events_cache.get(root)}")


# --------------------------------------------------------------------------- #
# _write_event_meta_sidecar_locked: signature None + OSError unlink fallback
# --------------------------------------------------------------------------- #
def test_write_event_meta_sidecar():
    ing = _fresh()
    root = "boot-wmeta"
    # Missing events file -> signature None -> no-op (309).
    ing._write_event_meta_sidecar_locked(
        root, Path(_events_path(root)),
        max_by_sid={"s1": 1}, render_by_sid={"s1": 1},
        root_events_version=1, root_events_candidate_version=0, seq=1,
    )
    assert not os.path.exists(_meta_path(root)), "missing signature -> no sidecar written"

    # Real file; monkeypatch os.replace to raise -> tmp unlink fallback.
    epath = _write_bytes(root, "events.jsonl", b'{"seq":1}\n')
    orig_replace = os.replace

    def _boom_replace(_src, _dst):
        raise OSError("boom")

    # Patch the module-level os.replace used by event_ingester.
    event_ingester.os.replace = _boom_replace  # type: ignore[attr-defined]
    try:
        ing._write_event_meta_sidecar_locked(
            root, Path(epath),
            max_by_sid={"s1": 1}, render_by_sid={"s1": 1},
            root_events_version=1, root_events_candidate_version=0, seq=1,
            root_events_by_sid={"s1": [{"seq": 1}]},
        )
    finally:
        event_ingester.os.replace = orig_replace  # type: ignore[attr-defined]
    # tmp file cleaned up by the unlink fallback (325-328).
    tmp = _meta_path(root) + ".tmp"
    assert not os.path.exists(tmp), f"OSError -> tmp unlinked -- {tmp}"
    assert not os.path.exists(_meta_path(root)), "OSError -> no sidecar finalized"

    # Nested unlink failure: tmp removal also raises -> swallowed (328-329).
    # Reaching here proves the nested OSError did not propagate.
    epath2 = _write_bytes("boot-wmeta2", "events.jsonl", b'{"seq":1}\n')
    root2 = "boot-wmeta2"
    orig_unlink = os.unlink
    event_ingester.os.replace = _boom_replace  # type: ignore[attr-defined]

    def _boom_unlink(_p):
        raise OSError("unlink boom")

    event_ingester.os.unlink = _boom_unlink  # type: ignore[attr-defined]
    try:
        ing._write_event_meta_sidecar_locked(
            root2, Path(epath2),
            max_by_sid={"s1": 1}, render_by_sid={"s1": 1},
            root_events_version=1, root_events_candidate_version=0, seq=1,
        )
    finally:
        event_ingester.os.replace = orig_replace  # type: ignore[attr-defined]
        event_ingester.os.unlink = orig_unlink  # type: ignore[attr-defined]
    # nested unlink OSError swallowed: control reached past the call.


# --------------------------------------------------------------------------- #
# _load_event_summaries_sidecar_locked + _valid_worker_rows / _valid_seq_offsets
# --------------------------------------------------------------------------- #
def test_valid_worker_rows():
    V = EventIngester._valid_worker_rows
    fs = 100
    assert V("not a dict", fs) is False, "non-dict -> False"
    assert V({123: []}, fs) is False, "non-str delegation_id -> False"
    assert V({"d": "notlist"}, fs) is False, "non-list spans -> False"
    assert V({"d": "notlist-inner"}, fs) is False, "span not list -> False"
    assert V({"d": [123]}, fs) is False, "span non-list elem -> False"
    assert V({"d": [[1]]}, fs) is False, "span len!=2 -> False"
    assert V({"d": [[1, "x"]]}, fs) is False, "span non-int -> False"
    assert V({"d": [[True, 2]]}, fs) is False, "span bool -> False"
    assert V({"d": [[10, 5]]}, fs) is False, "span start>=end -> False"
    assert V({"d": [[-1, 5]]}, fs) is False, "span negative -> False"
    assert V({"d": [[90, 200]]}, fs) is False, "span beyond file_size -> False"
    assert V({"d": [[0, 50], [50, 100]]}, fs) is True, "valid -> True"
    assert V({}, fs) is True, "empty dict -> True"


def test_valid_seq_offsets():
    V = EventIngester._valid_seq_offsets
    fs = 100
    assert V("nope", fs) is False, "non-list -> False (400)"
    assert V([1, "x"], fs) is False, "non-int elem -> False (403-404)"
    assert V([1, True], fs) is False, "bool elem -> False"
    assert V([10, 5], fs) is False, "non-monotonic -> False (405-406)"
    assert V([-1, 5], fs) is False, "negative offset -> False (407)"
    assert V([90, 200], fs) is False, "offset >= file_size -> False (408)"
    assert V([], 10) is False, "empty list + non-empty file -> False (410)"
    assert V([], 0) is True, "empty list + empty file -> True (410)"
    assert V([0, 10, 20], fs) is True, "valid monotonic -> True"


def test_load_event_summaries_sidecar():
    ing = _fresh()
    root = "boot-summ"
    # Missing events file -> signature None (336).
    ret = ing._load_event_summaries_sidecar_locked(
        root, Path(_events_path(root)), tail=5)
    assert ret is None, f"missing signature -> None -- {ret}"

    # Valid sidecar with a non-int resolution key (skipped) + a good one.
    epath = _write_bytes(root, "events.jsonl", b'{"seq":1}\n{"seq":2}\n')
    sig = EventIngester._event_file_signature(Path(epath))
    assert sig is not None
    sidecar = {
        "summary_version": event_ingester._EVENT_SUMMARIES_VERSION,
        "mtime_ns": sig[0], "size": sig[1], "tail": 5,
        "summaries": {"m1": {"byte_start": 0, "byte_end": 12}},
        "resolutions": {"not-an-int": "m1", "2": "m1", "3": 123},  # 365-366 skips first; 367 False skips non-str msg_id
        "seq_offsets": [0, 12],
        "worker_rows": {"d1": [[0, 12]]},
    }
    _write_bytes(root, "event_summaries.json", json.dumps(sidecar).encode())
    ret = ing._load_event_summaries_sidecar_locked(root, Path(epath), tail=5)
    assert ret is not None and ret[1] == {2: "m1"}, (
        f"valid load, non-int resolution skipped -- {ret}")
    assert ing._seq_offsets[root] == [0, 12], f"seq_offsets loaded -- {ing._seq_offsets.get(root)}"
    assert ing._worker_rows[root] == {"d1": [(0, 12)]}, f"worker_rows loaded -- {ing._worker_rows.get(root)}"

    # Stale (wrong tail) -> rejected.
    ing2 = _fresh()
    ret2 = ing2._load_event_summaries_sidecar_locked(root, Path(epath), tail=99)
    assert ret2 is None, f"tail mismatch -> None -- {ret2}"

    # Bad worker_rows shape -> rejected.
    ing3 = _fresh()
    sidecar["worker_rows"] = {"d1": [[999, 50]]}  # start>=end -> invalid
    _write_bytes(root, "event_summaries.json", json.dumps(sidecar).encode())
    ret3 = ing3._load_event_summaries_sidecar_locked(root, Path(epath), tail=5)
    assert ret3 is None, f"invalid worker_rows -> None -- {ret3}"


# --------------------------------------------------------------------------- #
# _write_event_summaries_sidecar_locked: signature None + guard skip + OSError
# --------------------------------------------------------------------------- #
def test_write_event_summaries_sidecar():
    ing = _fresh()
    root = "boot-wsumm"
    # Missing events file -> signature None -> no-op (423).
    ing._write_event_summaries_sidecar_locked(
        root, Path(_events_path(root)), tail=5,
        summaries={"m1": {}}, resolutions={1: "m1"})
    assert not os.path.exists(_summaries_path(root)), "missing signature -> no write"

    # Guard mismatch: seq_offsets absent -> skip (425-431).
    epath = _write_bytes(root, "events.jsonl", b'{"seq":1}\n')
    ing._write_event_summaries_sidecar_locked(
        root, Path(epath), tail=5,
        summaries={"m1": {}}, resolutions={1: "m1"})
    assert not os.path.exists(_summaries_path(root)), "no seq_offsets -> guard skip"

    # Seed valid offsets, then OSError on os.replace -> tmp unlink (452-456).
    ing._seq_offsets[root] = [0]
    ing._next_offset[root] = len(b'{"seq":1}\n')
    ing._seq[root] = 1
    ing._worker_rows[root] = {"d1": [(0, 10)]}
    orig_replace = os.replace
    event_ingester.os.replace = lambda _s, _d: (_ for _ in ()).throw(OSError("x"))  # type: ignore[attr-defined]
    try:
        ing._write_event_summaries_sidecar_locked(
            root, Path(epath), tail=5,
            summaries={"m1": {}}, resolutions={1: "m1"})
    finally:
        event_ingester.os.replace = orig_replace  # type: ignore[attr-defined]
    tmp = _summaries_path(root) + ".tmp"
    assert not os.path.exists(tmp), "OSError -> tmp cleaned"
    assert not os.path.exists(_summaries_path(root)), "OSError -> no final sidecar"

    # Nested unlink failure: tmp removal also raises -> swallowed (455-456).
    # Reaching here proves the nested OSError did not propagate.
    ing2 = _fresh()
    ing2._seq_offsets[root] = [0]
    ing2._next_offset[root] = len(b'{"seq":1}\n')
    ing2._seq[root] = 1
    orig_unlink = os.unlink
    event_ingester.os.replace = lambda _s, _d: (_ for _ in ()).throw(OSError("x"))  # type: ignore[attr-defined]
    event_ingester.os.unlink = lambda _p: (_ for _ in ()).throw(OSError("unlink boom"))  # type: ignore[attr-defined]
    try:
        ing2._write_event_summaries_sidecar_locked(
            root, Path(epath), tail=5,
            summaries={"m1": {}}, resolutions={1: "m1"})
    finally:
        event_ingester.os.replace = orig_replace  # type: ignore[attr-defined]
        event_ingester.os.unlink = orig_unlink  # type: ignore[attr-defined]
    # summaries nested unlink OSError swallowed: control reached past the call.


# --------------------------------------------------------------------------- #
# _close_handle_locked: fsync OSError on drain (479-480)
# --------------------------------------------------------------------------- #
def test_close_handle_fsync_oserror():
    ing = _fresh()
    root = "boot-closefsync"
    epath = _write_bytes(root, "events.jsonl", b'{"seq":1}\n')
    ing._open_append_handle(root, Path(epath))
    orig_fsync = os.fsync
    calls = {"n": 0}

    def _boom_fsync(_fd):
        calls["n"] += 1
        raise OSError("fsync boom")

    event_ingester.os.fsync = _boom_fsync  # type: ignore[attr-defined]
    try:
        ing._close_handle_locked(root)  # must not raise
    finally:
        event_ingester.os.fsync = orig_fsync  # type: ignore[attr-defined]
    assert calls["n"] >= 1, f"fsync attempted on drain -- {calls}"
    assert root not in ing._handles, "handle popped despite fsync OSError"


# --------------------------------------------------------------------------- #
# _prune_append_handles: victim_lock-None skip + victim_id-None return
# --------------------------------------------------------------------------- #
def test_prune_append_handles_no_locks():
    ing = _fresh()
    # Over-cap handles, but NO per-root locks -> every victim's
    # victim_lock is None -> skipped (502-503) until victim_id is None (499).
    for i in range(event_ingester._MAX_OPEN_APPEND_HANDLES + 5):
        rid = f"boot-prune-{i}"
        ing._handles[rid] = (Path("/nonexistent") / f"{rid}.jsonl", None)
    # _locks deliberately left empty.
    ing._prune_append_handles(exclude_root_id="boot-prune-0")
    assert len(ing._handles) == event_ingester._MAX_OPEN_APPEND_HANDLES + 5, (
        f"no-lock victims all skipped -> cache unchanged -- {len(ing._handles)}")


# --------------------------------------------------------------------------- #
# _fsync_dirty_now: fh-None skip + fsync OSError (580, 584-585)
# --------------------------------------------------------------------------- #
def test_fsync_dirty_now():
    ing = _fresh()
    # Dirty root with no open handle -> fh None -> skip (580).
    ing._fsync_dirty.add("boot-fsyncnone")
    ing._fsync_dirty_now()  # must not raise
    assert "boot-fsyncnone" not in ing._fsync_dirty, "fh-None root drained from dirty set"

    # Dirty root with a real handle, fsync raises -> logged, not raised (584-585).
    root = "boot-fsyncerr"
    epath = _write_bytes(root, "events.jsonl", b'{"seq":1}\n')
    ing._open_append_handle(root, Path(epath))
    ing._fsync_dirty.add(root)
    orig_fsync = os.fsync
    event_ingester.os.fsync = lambda _fd: (_ for _ in ()).throw(OSError("x"))  # type: ignore[attr-defined]
    try:
        ing._fsync_dirty_now()  # must not raise
    finally:
        event_ingester.os.fsync = orig_fsync  # type: ignore[attr-defined]
    assert root not in ing._fsync_dirty, "OSError root drained from dirty set"
    ing._close_handle_locked(root)


# --------------------------------------------------------------------------- #
# _ensure_open: torn-tail recovery (blank line reset + partial trailing JSON)
# --------------------------------------------------------------------------- #
def test_ensure_open_torn_tail():
    ing = _fresh()
    root = "boot-torn"
    # Valid line, a blank line, a valid line, then a torn trailing partial.
    body = (
        b'{"seq":1,"sid":"s1","type":"agent_message","data":{"uuid":"u1"},"msg_id":"m1"}\n'
        b'\n'                       # blank -> torn_offset reset (635-636)
        b'{"seq":2,"sid":"s1","type":"agent_message","data":{"uuid":"u2"},"msg_id":"m2"}\n'
        b'{"seq":3,this-is-torn\n'  # first bad line -> torn_offset set (641)
        b'{"seq":4,also-torn'       # second consecutive bad -> 640->642 branch
    )
    epath = Path(_write_bytes(root, "events.jsonl", body))
    original_size = epath.stat().st_size
    assert original_size == len(body), "fixture written whole"

    path, fh = ing._ensure_open(root)
    # Torn tail truncated: file now ends after line 2.
    new_size = path.stat().st_size
    assert new_size < original_size, f"torn tail truncated -- {new_size} < {original_size}"
    # Two valid entries seeded; next ingest gets seq 3 (not 4).
    assert ing._seq[root] == 2, f"two valid entries seeded -- {ing._seq.get(root)}"
    s3 = ing.ingest(root, sid="s1", event_type="agent_message",
                    data={"uuid": "u3"}, source="t", msg_id="m3")
    assert s3 == 3, f"next ingest seq == 3 after truncation -- {s3}"
    ing._close_handle_locked(root)


TESTS = [
    test_ref_ctx_for_root,
    test_event_file_signature_oserror,
    test_seed_write_caches_locked,
    test_load_event_meta_sidecar,
    test_write_event_meta_sidecar,
    test_valid_worker_rows,
    test_valid_seq_offsets,
    test_load_event_summaries_sidecar,
    test_write_event_summaries_sidecar,
    test_close_handle_fsync_oserror,
    test_prune_append_handles_no_locks,
    test_fsync_dirty_now,
    test_ensure_open_torn_tail,
]


def main() -> None:
    failed = 0
    total = len(TESTS)
    for test in TESTS:
        print(f"\n--- {test.__name__} ---")
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"{FAIL} {test.__name__}: {exc}")
        except Exception:
            failed += 1
            import traceback
            traceback.print_exc()
        else:
            print(f"{PASS} {test.__name__}")
    print(f"\n{total - failed}/{total} test groups passed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
