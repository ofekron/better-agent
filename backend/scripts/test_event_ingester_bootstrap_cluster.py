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


def _check(cond: bool, name: str, detail: str = "") -> bool:
    print(f"{PASS if cond else FAIL} {name}{'' if cond else ' -- ' + detail}")
    return cond


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
def test_ref_ctx_for_root() -> bool:
    ok = True
    # Primary node, real cwd -> (cwd, False).
    sess = session_store.create_session(
        name="boot", cwd="/tmp", node_id="primary", id="boot-refctx-primary",
    )
    cwd, is_remote = event_ingester._ref_ctx_for_root(sess["id"])
    ok = _check(cwd == "/tmp" and is_remote is False,
                "primary session -> (cwd, False)", f"{cwd!r},{is_remote!r}") and ok
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
    ok = _check(cwd2 is None and is_remote2 is False,
                "empty cwd -> (None, False)", f"{cwd2!r},{is_remote2!r}") and ok
    # Non-primary node -> assume_exists True (files live on the node).
    sess3 = session_store.create_session(
        name="boot3", cwd="/tmp", node_id="node-remote", id="boot-refctx-remote",
    )
    cwd3, is_remote3 = event_ingester._ref_ctx_for_root(sess3["id"])
    ok = _check(cwd3 == "/tmp" and is_remote3 is True,
                "non-primary node -> (cwd, True)", f"{cwd3!r},{is_remote3!r}") and ok
    # Lookup raises -> defensive (None, False).
    orig = session_store.summary_fields_many

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    session_store.summary_fields_many = _boom  # type: ignore[assignment]
    try:
        cwd4, is_remote4 = event_ingester._ref_ctx_for_root("never")
    finally:
        session_store.summary_fields_many = orig  # type: ignore[assignment]
    ok = _check(cwd4 is None and is_remote4 is False,
                "lookup exception -> (None, False)", f"{cwd4!r},{is_remote4!r}") and ok
    # Unknown root (no summary) -> (None, False) via the dict-miss path.
    cwd5, is_remote5 = event_ingester._ref_ctx_for_root("does-not-exist-sid")
    ok = _check(cwd5 is None and is_remote5 is False,
                "unknown root -> (None, False)", f"{cwd5!r},{is_remote5!r}") and ok
    return ok


# --------------------------------------------------------------------------- #
# _event_file_signature: stat OSError
# --------------------------------------------------------------------------- #
def test_event_file_signature_oserror() -> bool:
    ok = True
    # A path whose parent does not exist -> stat raises OSError -> None.
    missing = Path(_root_dir("sig-missing")) / "events.jsonl"
    ok = _check(
        EventIngester._event_file_signature(missing) is None,
        "stat OSError -> None", f"{missing}",
    ) and ok
    ok = _check(
        EventIngester._event_file_identity(missing) is None,
        "identity stat OSError -> None", f"{missing}",
    ) and ok
    # Existing file -> real (mtime_ns, size) tuple.
    real = Path(_write_bytes("sig-ok", "events.jsonl", b"hi\n"))
    sig = EventIngester._event_file_signature(real)
    ok = _check(sig is not None and sig[1] == 3,
                "existing file signature", f"{sig}") and ok
    return ok


# --------------------------------------------------------------------------- #
# _seed_write_caches_locked: hash fallback, sid/seq skip, projection folding
# --------------------------------------------------------------------------- #
def test_seed_write_caches_locked() -> bool:
    ok = True
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
    ok = _check(ing._seq[root] == 7, "seq seeded to entry count",
                f"{ing._seq[root]}") and ok
    ok = _check(ing._seq_offsets[root] == seq_offsets, "seq_offsets seeded") and ok
    # max_seq_by_sid only captures valid sid/seq (entries 2,3 skipped).
    ok = _check(ing._max_seq_by_sid[root] == {"s1": 7},
                "max_seq_by_sid s1==7", f"{ing._max_seq_by_sid[root]}") and ok
    # render projection skips user_message but INCLUDES ownership_resolved
    # (it is in the render set), so s1 max = max(seq 1, 3, 4, 6) = 6.
    ok = _check(ing._render_seq_by_sid[root] == {"s1": 6},
                "render_seq s1==6 (agent+manager+ownership)",
                f"{ing._render_seq_by_sid[root]}") and ok
    # candidate version: seq 3 was a candidate, then resolved -> folds to 0.
    ok = _check(ing._root_events_candidate_version[root] == 0,
                "resolved candidate folds to 0",
                f"{ing._root_events_candidate_version[root]}") and ok
    # root_events_version counts every root-projection row (agent/manager/
    # ownership_resolved): entries 1,4,5,6 = 4 (2/3 skipped, 7 non-projection).
    ok = _check(ing._root_events_version[root] == 4,
                "root_events_version == 4", f"{ing._root_events_version[root]}") and ok
    # mixed-key fallback hash present in seen set (uid-keyed).
    seen = ing._seen_uuids[root]
    ok = _check(any(k.startswith("u1:") for k in seen),
                "mixed-key data hashed via fallback", f"{seen}") and ok
    return ok


# --------------------------------------------------------------------------- #
# _load_event_meta_sidecar_locked
# --------------------------------------------------------------------------- #
def test_load_event_meta_sidecar() -> bool:
    ok = True
    ing = _fresh()
    root = "boot-meta"
    # Missing events file -> signature None -> None (253).
    ret = ing._load_event_meta_sidecar_locked(root, Path(_events_path(root)))
    ok = _check(ret is None, "missing signature -> None", f"{ret}") and ok

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
    ok = _check(ret == {"s1": 2}, "valid load returns max_by_sid",
                f"{ret}") and ok
    ok = _check(ing._seq[root] == 2, "seq loaded from sidecar",
                f"{ing._seq.get(root)}") and ok
    ok = _check(ing._root_events_version[root] == 5,
                "root_events_version loaded", f"{ing._root_events_version[root]}") and ok
    # root_events_cache populated (282-291).
    cached = ing._root_events_cache.get(root)
    ok = _check(cached is not None and cached[0] == 5 and "s1" in cached[1],
                "root_events_cache populated", f"{cached}") and ok

    # Stale signature -> rejected.
    ing2 = _fresh()
    sidecar["mtime_ns"] = sidecar["mtime_ns"] + 9999
    _write_bytes(root, "event_meta.json", json.dumps(sidecar).encode())
    ret2 = ing2._load_event_meta_sidecar_locked(root, Path(epath))
    ok = _check(ret2 is None, "stale signature -> None", f"{ret2}") and ok

    # Non-dict root_events_by_sid -> projection skipped (282 False branch).
    ing3 = _fresh()
    sidecar2 = dict(sidecar)
    sidecar2["mtime_ns"] = sig[0]
    sidecar2["root_events_by_sid"] = "not-a-dict"
    _write_bytes(root, "event_meta.json", json.dumps(sidecar2).encode())
    ret3 = ing3._load_event_meta_sidecar_locked(root, Path(epath))
    ok = _check(ret3 == {"s1": 2}, "non-dict root_events still loads max_by_sid",
                f"{ret3}") and ok
    ok = _check(ing3._root_events_cache.get(root) is None,
                "non-dict root_events -> no cache entry",
                f"{ing3._root_events_cache.get(root)}") and ok
    return ok


# --------------------------------------------------------------------------- #
# _write_event_meta_sidecar_locked: signature None + OSError unlink fallback
# --------------------------------------------------------------------------- #
def test_write_event_meta_sidecar() -> bool:
    ok = True
    ing = _fresh()
    root = "boot-wmeta"
    # Missing events file -> signature None -> no-op (309).
    ing._write_event_meta_sidecar_locked(
        root, Path(_events_path(root)),
        max_by_sid={"s1": 1}, render_by_sid={"s1": 1},
        root_events_version=1, root_events_candidate_version=0, seq=1,
    )
    ok = _check(not os.path.exists(_meta_path(root)),
                "missing signature -> no sidecar written") and ok

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
    ok = _check(not os.path.exists(tmp),
                "OSError -> tmp unlinked", f"{tmp}") and ok
    ok = _check(not os.path.exists(_meta_path(root)),
                "OSError -> no sidecar finalized") and ok

    # Nested unlink failure: tmp removal also raises -> swallowed (328-329).
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
    ok = _check(True, "nested unlink OSError swallowed (no raise)") and ok
    return ok


# --------------------------------------------------------------------------- #
# _load_event_summaries_sidecar_locked + _valid_worker_rows / _valid_seq_offsets
# --------------------------------------------------------------------------- #
def test_valid_worker_rows() -> bool:
    ok = True
    V = EventIngester._valid_worker_rows
    fs = 100
    ok = _check(V("not a dict", fs) is False, "non-dict -> False") and ok
    ok = _check(V({123: []}, fs) is False, "non-str delegation_id -> False") and ok
    ok = _check(V({"d": "notlist"}, fs) is False, "non-list spans -> False") and ok
    ok = _check(V({"d": "notlist-inner"}, fs) is False, "span not list -> False") and ok
    ok = _check(V({"d": [123]}, fs) is False, "span non-list elem -> False") and ok
    ok = _check(V({"d": [[1]]}, fs) is False, "span len!=2 -> False") and ok
    ok = _check(V({"d": [[1, "x"]]}, fs) is False, "span non-int -> False") and ok
    ok = _check(V({"d": [[True, 2]]}, fs) is False, "span bool -> False") and ok
    ok = _check(V({"d": [[10, 5]]}, fs) is False, "span start>=end -> False") and ok
    ok = _check(V({"d": [[-1, 5]]}, fs) is False, "span negative -> False") and ok
    ok = _check(V({"d": [[90, 200]]}, fs) is False, "span beyond file_size -> False") and ok
    ok = _check(V({"d": [[0, 50], [50, 100]]}, fs) is True, "valid -> True") and ok
    ok = _check(V({}, fs) is True, "empty dict -> True") and ok
    return ok


def test_valid_seq_offsets() -> bool:
    ok = True
    V = EventIngester._valid_seq_offsets
    fs = 100
    ok = _check(V("nope", fs) is False, "non-list -> False (400)") and ok
    ok = _check(V([1, "x"], fs) is False, "non-int elem -> False (403-404)") and ok
    ok = _check(V([1, True], fs) is False, "bool elem -> False") and ok
    ok = _check(V([10, 5], fs) is False, "non-monotonic -> False (405-406)") and ok
    ok = _check(V([-1, 5], fs) is False, "negative offset -> False (407)") and ok
    ok = _check(V([90, 200], fs) is False, "offset >= file_size -> False (408)") and ok
    ok = _check(V([], 10) is False, "empty list + non-empty file -> False (410)") and ok
    ok = _check(V([], 0) is True, "empty list + empty file -> True (410)") and ok
    ok = _check(V([0, 10, 20], fs) is True, "valid monotonic -> True") and ok
    return ok


def test_load_event_summaries_sidecar() -> bool:
    ok = True
    ing = _fresh()
    root = "boot-summ"
    # Missing events file -> signature None (336).
    ret = ing._load_event_summaries_sidecar_locked(
        root, Path(_events_path(root)), tail=5)
    ok = _check(ret is None, "missing signature -> None", f"{ret}") and ok

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
    ok = _check(ret is not None and ret[1] == {2: "m1"},
                "valid load, non-int resolution skipped",
                f"{ret}") and ok
    ok = _check(ing._seq_offsets[root] == [0, 12],
                "seq_offsets loaded", f"{ing._seq_offsets.get(root)}") and ok
    ok = _check(ing._worker_rows[root] == {"d1": [(0, 12)]},
                "worker_rows loaded", f"{ing._worker_rows.get(root)}") and ok

    # Stale (wrong tail) -> rejected.
    ing2 = _fresh()
    ret2 = ing2._load_event_summaries_sidecar_locked(root, Path(epath), tail=99)
    ok = _check(ret2 is None, "tail mismatch -> None", f"{ret2}") and ok

    # Bad worker_rows shape -> rejected.
    ing3 = _fresh()
    sidecar["worker_rows"] = {"d1": [[999, 50]]}  # start>=end -> invalid
    _write_bytes(root, "event_summaries.json", json.dumps(sidecar).encode())
    ret3 = ing3._load_event_summaries_sidecar_locked(root, Path(epath), tail=5)
    ok = _check(ret3 is None, "invalid worker_rows -> None", f"{ret3}") and ok
    return ok


# --------------------------------------------------------------------------- #
# _write_event_summaries_sidecar_locked: signature None + guard skip + OSError
# --------------------------------------------------------------------------- #
def test_write_event_summaries_sidecar() -> bool:
    ok = True
    ing = _fresh()
    root = "boot-wsumm"
    # Missing events file -> signature None -> no-op (423).
    ing._write_event_summaries_sidecar_locked(
        root, Path(_events_path(root)), tail=5,
        summaries={"m1": {}}, resolutions={1: "m1"})
    ok = _check(not os.path.exists(_summaries_path(root)),
                "missing signature -> no write") and ok

    # Guard mismatch: seq_offsets absent -> skip (425-431).
    epath = _write_bytes(root, "events.jsonl", b'{"seq":1}\n')
    ing._write_event_summaries_sidecar_locked(
        root, Path(epath), tail=5,
        summaries={"m1": {}}, resolutions={1: "m1"})
    ok = _check(not os.path.exists(_summaries_path(root)),
                "no seq_offsets -> guard skip") and ok

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
    ok = _check(not os.path.exists(tmp), "OSError -> tmp cleaned") and ok
    ok = _check(not os.path.exists(_summaries_path(root)),
                "OSError -> no final sidecar") and ok

    # Nested unlink failure: tmp removal also raises -> swallowed (455-456).
    ing2 = _fresh()
    ing2._seq_offsets[root] = [0]
    ing2._next_offset[root] = len(b'{"seq":1}\n')
    ing2._seq[root] = 1
    orig_unlink = os.unlink
    event_ingester.os.replace = lambda _s, _d: (_ for _ in ()).throw(OSError("x"))  # type: ignore[attr-defined]
    event_ingester.os.unlink = lambda _p: (_ for _ in ()).throw(OSError("unlink boom"))  # type: ignore[attr-defined]
    try:
        ing2._write_event_summaries_sidecar_locked(  # must not raise
            root, Path(epath), tail=5,
            summaries={"m1": {}}, resolutions={1: "m1"})
    finally:
        event_ingester.os.replace = orig_replace  # type: ignore[attr-defined]
        event_ingester.os.unlink = orig_unlink  # type: ignore[attr-defined]
    ok = _check(True, "summaries nested unlink OSError swallowed") and ok
    return ok


# --------------------------------------------------------------------------- #
# _close_handle_locked: fsync OSError on drain (479-480)
# --------------------------------------------------------------------------- #
def test_close_handle_fsync_oserror() -> bool:
    ok = True
    ing = _fresh()
    root = "boot-closefsync"
    epath = _write_bytes(root, "events.jsonl", b'{"seq":1}\n')
    fh = ing._open_append_handle(root, Path(epath))
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
    ok = _check(calls["n"] >= 1, "fsync attempted on drain", f"{calls}") and ok
    ok = _check(root not in ing._handles, "handle popped despite fsync OSError") and ok
    return ok


# --------------------------------------------------------------------------- #
# _prune_append_handles: victim_lock-None skip + victim_id-None return
# --------------------------------------------------------------------------- #
def test_prune_append_handles_no_locks() -> bool:
    ok = True
    ing = _fresh()
    # Over-cap handles, but NO per-root locks -> every victim's
    # victim_lock is None -> skipped (502-503) until victim_id is None (499).
    for i in range(event_ingester._MAX_OPEN_APPEND_HANDLES + 5):
        rid = f"boot-prune-{i}"
        ing._handles[rid] = (Path("/nonexistent") / f"{rid}.jsonl", None)
    # _locks deliberately left empty.
    ing._prune_append_handles(exclude_root_id="boot-prune-0")
    ok = _check(
        len(ing._handles) == event_ingester._MAX_OPEN_APPEND_HANDLES + 5,
        "no-lock victims all skipped -> cache unchanged",
        f"{len(ing._handles)}") and ok
    return ok


# --------------------------------------------------------------------------- #
# _fsync_dirty_now: fh-None skip + fsync OSError (580, 584-585)
# --------------------------------------------------------------------------- #
def test_fsync_dirty_now() -> bool:
    ok = True
    ing = _fresh()
    # Dirty root with no open handle -> fh None -> skip (580).
    ing._fsync_dirty.add("boot-fsyncnone")
    ing._fsync_dirty_now()  # must not raise
    ok = _check("boot-fsyncnone" not in ing._fsync_dirty,
                "fh-None root drained from dirty set") and ok

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
    ok = _check(root not in ing._fsync_dirty,
                "OSError root drained from dirty set") and ok
    ing._close_handle_locked(root)
    return ok


# --------------------------------------------------------------------------- #
# _ensure_open: torn-tail recovery (blank line reset + partial trailing JSON)
# --------------------------------------------------------------------------- #
def test_ensure_open_torn_tail() -> bool:
    ok = True
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
    ok = _check(original_size == len(body), "fixture written whole") and ok

    path, fh = ing._ensure_open(root)
    # Torn tail truncated: file now ends after line 2.
    new_size = path.stat().st_size
    ok = _check(new_size < original_size, "torn tail truncated",
                f"{new_size} < {original_size}") and ok
    # Two valid entries seeded; next ingest gets seq 3 (not 4).
    ok = _check(ing._seq[root] == 2, "two valid entries seeded",
                f"{ing._seq.get(root)}") and ok
    s3 = ing.ingest(root, sid="s1", event_type="agent_message",
                    data={"uuid": "u3"}, source="t", msg_id="m3")
    ok = _check(s3 == 3, "next ingest seq == 3 after truncation", f"{s3}") and ok
    ing._close_handle_locked(root)
    return ok


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
