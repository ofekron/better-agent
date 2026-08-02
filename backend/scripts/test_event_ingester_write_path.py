"""Unit coverage for the write-side cluster of event_ingester.py:
_ingest_impl (dedupe_by_uid_only short-circuit / owner-aware dedup via
_is_duplicate_event_owner / json.dumps sort_keys exception fallback),
ingest_batch (empty / owner dedup / no-uid row / sort_keys exception),
cursor (missing file / unknown root with a file), would_dedupe_orphan
(sort_keys exception fallback), and _emit error-suppression branches
(hydration-index receipt failure + search-projection enqueue failure).

Run with:
    cd backend && .venv/bin/python scripts/test_event_ingester_write_path.py
"""

from __future__ import annotations

import json
import os
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ingester-write-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import hydration_index_store  # noqa: E402
import session_search_projection  # noqa: E402
import event_ingester  # noqa: E402
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


def _append_raw(root_id: str, entry: dict) -> None:
    path = _events_path(root_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _fresh() -> EventIngester:
    return EventIngester()


# --------------------------------------------------------------------------- #
# _ingest_impl: dedupe_by_uid_only short-circuit (line 916-917)
# --------------------------------------------------------------------------- #
def test_dedupe_by_uid_only() -> bool:
    ok = True
    ing = _fresh()
    root, sid = "w-uidonly", "sid-A"
    s1 = ing.ingest(root, sid=sid, event_type="agent_message",
                    data=_agent("u1"), source="t", msg_id="m1",
                    dedupe_by_uid_only=True)
    ok = _check(s1 == 1, "first uid-only ingest writes (seq 1)", f"{s1}") and ok
    # Same uid again under dedupe_by_uid_only -> -1 (skip), regardless of data
    # shape or msg_id.
    s2 = ing.ingest(root, sid=sid, event_type="agent_message",
                    data=_agent("u1", "changed"), source="t", msg_id="m2",
                    dedupe_by_uid_only=True)
    ok = _check(s2 == -1, "repeat uid under dedupe_by_uid_only -> -1", f"{s2}") and ok
    # A DIFFERENT uid still writes.
    s3 = ing.ingest(root, sid=sid, event_type="agent_message",
                    data=_agent("u2"), source="t", msg_id="m1",
                    dedupe_by_uid_only=True)
    ok = _check(s3 == 2, "different uid under dedupe_by_uid_only writes (seq 2)",
                f"{s3}") and ok
    return ok


# --------------------------------------------------------------------------- #
# _is_duplicate_event_owner: same-msg_id dup, orphan-vs-owned, distinct owners
# --------------------------------------------------------------------------- #
def test_owner_aware_dedup() -> bool:
    ok = True
    ing = _fresh()
    root, sid = "w-owner", "sid-A"

    # Same msg_id re-emitting identical data -> dedup (-1).
    s1 = ing.ingest(root, sid=sid, event_type="agent_message",
                    data=_agent("a1"), source="t", msg_id="m1")
    s2 = ing.ingest(root, sid=sid, event_type="agent_message",
                    data=_agent("a1"), source="t", msg_id="m1")
    ok = _check(s1 == 1 and s2 == -1, "same msg_id identical data -> -1",
                f"{s1} {s2}") and ok

    # Orphan (msg_id=None) re-emit of an event ALREADY written under any
    # owner -> dedup (-1).
    s3 = ing.ingest(root, sid=sid, event_type="agent_message",
                    data=_agent("a2"), source="t", msg_id="m1")
    s4 = ing.ingest(root, sid=sid, event_type="agent_message",
                    data=_agent("a2"), source="t", msg_id=None)
    ok = _check(s3 == 2 and s4 == -1, "orphan after owned write -> -1",
                f"{s3} {s4}") and ok

    # The SAME event emitted under a DIFFERENT owner (msg_id) is NOT a dup —
    # dual-writer convergence lets both owners record the same event.
    s5 = ing.ingest(root, sid=sid, event_type="agent_message",
                    data=_agent("a3"), source="t", msg_id="m1")
    s6 = ing.ingest(root, sid=sid, event_type="agent_message",
                    data=_agent("a3"), source="t", msg_id="m2")
    ok = _check(s5 == 3 and s6 == 4, "distinct owners of same event both write",
                f"{s5} {s6}") and ok
    return ok


# --------------------------------------------------------------------------- #
# _ingest_impl: json.dumps(sort_keys=True) exception fallback (922-923).
# A dict with mixed-type keys (int + str) makes sort_keys raise TypeError,
# while _emit's sort_keys-less json.dumps still serializes it (int key -> str)
# — so the ingest completes while exercising the hash fallback.
# --------------------------------------------------------------------------- #
def test_ingest_sort_keys_exception() -> bool:
    ok = True
    ing = _fresh()
    root, sid = "w-sortkeys", "sid-A"
    mixed = {1: "a", "uuid": "u-mix"}  # int + str keys
    seq = ing.ingest(root, sid=sid, event_type="agent_message",
                     data=mixed, source="t", msg_id="m1")
    ok = _check(seq == 1, "mixed-key ingest writes (seq 1)", f"{seq}") and ok
    out, _, _ = ing.read_events(root)
    ok = _check(len(out) == 1, "mixed-key row persisted", f"{len(out)}") and ok
    # Re-ingesting the SAME mixed-key data dedups via the fallback hash too.
    seq2 = ing.ingest(root, sid=sid, event_type="agent_message",
                      data=mixed, source="t", msg_id="m1")
    ok = _check(seq2 == -1, "repeat mixed-key dedups via fallback hash", f"{seq2}") and ok
    return ok


# --------------------------------------------------------------------------- #
# _emit: run_id is recorded on the persisted entry when provided (line 766)
# --------------------------------------------------------------------------- #
def test_emit_run_id_recorded() -> bool:
    ok = True
    ing = _fresh()
    seq = ing.ingest("w-runid", sid="sid-A", event_type="agent_message",
                     data=_agent("r1"), source="t", msg_id="m1", run_id="run-42")
    ok = _check(seq == 1, "run_id ingest writes (seq 1)", f"{seq}") and ok
    out, _, _ = ing.read_events("w-runid")
    ok = _check(out[0].get("run_id") == "run-42", "entry carries run_id",
                f"{out[0].get('run_id')}") and ok
    return ok


# --------------------------------------------------------------------------- #
# _canonical_data_for_storage: file-ref rewrite failure falls back to a
# deepcopy of the original data (line 694-696). Ingest still succeeds.
# --------------------------------------------------------------------------- #
def test_canonical_rewrite_failure_falls_back_to_deepcopy() -> bool:
    ok = True
    orig = event_ingester.rewrite_event_data_isolated

    def _boom(*a, **k):
        raise RuntimeError("rewrite boom")

    event_ingester.rewrite_event_data_isolated = _boom  # type: ignore[assignment]
    try:
        ing = _fresh()
        seq = ing.ingest("w-rewrite", sid="sid-A", event_type="agent_message",
                         data=_agent("rw1"), source="t", msg_id="m1")
        ok = _check(seq == 1, "write succeeds despite rewrite failure", f"{seq}") and ok
        out, _, _ = ing.read_events("w-rewrite")
        ok = _check(len(out) == 1, "row persisted via deepcopy fallback",
                    f"{len(out)}") and ok
    finally:
        event_ingester.rewrite_event_data_isolated = orig  # type: ignore[assignment]
    return ok


# --------------------------------------------------------------------------- #
# _extract_uuid: all three event wrapper shapes that determine dedup identity.
# --------------------------------------------------------------------------- #
def test_extract_uuid_shapes() -> bool:
    ok = True
    extract = EventIngester._extract_uuid
    ok = _check(extract({"uuid": "top"}) == "top", "top-level uuid") and ok
    # Raw enriched claude line: {"data": {"uuid": ...}}
    ok = _check(extract({"data": {"uuid": "inner"}}) == "inner",
                "nested data.uuid") and ok
    # Orchestrator WS wrapper: {"event": {"data": {"uuid": ...}}}
    ok = _check(extract({"event": {"data": {"uuid": "wrapped"}}}) == "wrapped",
                "nested event.data.uuid") and ok
    ok = _check(extract({"no": "uuid"}) is None, "no uuid -> None") and ok
    ok = _check(extract({"data": {"noud": 1}}) is None, "inner without uuid -> None") and ok
    return ok


# --------------------------------------------------------------------------- #
# ingest_batch: empty -> []
# --------------------------------------------------------------------------- #
def test_ingest_batch_empty() -> bool:
    ing = _fresh()
    return _check(ing.ingest_batch("w-batch-empty", []) == [],
                  "empty batch -> []")


# --------------------------------------------------------------------------- #
# ingest_batch: dedup -> -1 in result, no-uid row, sort_keys exception, success
# --------------------------------------------------------------------------- #
def test_ingest_batch_mixed() -> bool:
    ok = True
    ing = _fresh()
    root, sid = "w-batch", "sid-A"
    events = [
        # (sid, event_type, data, source, run_id, msg_id)
        (sid, "agent_message", _agent("b1"), "t", None, "m1"),
        # duplicate of the first -> -1
        (sid, "agent_message", _agent("b1"), "t", None, "m1"),
        # no-uid metadata-style event -> written, exercises the `if uid` False
        (sid, "pr_link", {"url": "https://x/y"}, "t", None, None),
        # mixed-key data -> sort_keys exception fallback inside the batch path
        (sid, "agent_message", {1: "z", "uuid": "b-mix"}, "t", None, "m2"),
    ]
    seqs = ing.ingest_batch(root, events)
    ok = _check(seqs[0] == 1, "batch row 0 writes (seq 1)", f"{seqs}") and ok
    ok = _check(seqs[1] == -1, "batch row 1 dedups (-1)", f"{seqs}") and ok
    ok = _check(seqs[2] == 2, "batch no-uid row writes (seq 2)", f"{seqs}") and ok
    ok = _check(seqs[3] == 3, "batch mixed-key row writes via fallback (seq 3)",
                f"{seqs}") and ok
    return ok


# --------------------------------------------------------------------------- #
# cursor: unknown root with no file -> 0; unknown root WITH a file -> counted
# --------------------------------------------------------------------------- #
def test_cursor_paths() -> bool:
    ok = True
    ing = _fresh()
    ok = _check(ing.cursor("w-cursor-none") == 0,
                "cursor unknown root, no file -> 0") and ok
    # A root with an on-disk file but never ingested/scanned by this instance
    # -> cursor counts the lines and caches _seq.
    root = "w-cursor-file"
    for i in range(3):
        _append_raw(root, {"seq": i + 1, "ts": "t", "sid": "sid-A",
                           "type": "agent_message", "data": _agent(f"c{i}"),
                           "source": "t", "msg_id": "m1"})
    cur = ing.cursor(root)
    ok = _check(cur == 3, "cursor counts 3 on-disk lines", f"{cur}") and ok
    ok = _check(ing._seq.get(root) == 3, "cursor cached _seq == 3",
                f"{ing._seq.get(root)}") and ok
    return ok


# --------------------------------------------------------------------------- #
# would_dedupe_orphan: sort_keys exception fallback (1783-1784) via mixed keys
# --------------------------------------------------------------------------- #
def test_would_dedupe_orphan_sort_keys_exception() -> bool:
    ok = True
    ing = _fresh()
    root, sid = "w-wdo", "sid-A"
    mixed = {1: "a", "uuid": "u-wdo"}
    # Unknown hash -> not a dedup (no write performed; pure pre-flight check).
    ok = _check(ing.would_dedupe_orphan(root, "agent_message", mixed) is False,
                "mixed-key orphan not yet seen -> False") and ok
    # After an authoritative ingest of the same mixed-key data, the orphan
    # pre-flight must recognize it as a dedup via the fallback hash.
    ing.ingest(root, sid=sid, event_type="agent_message",
               data=mixed, source="t", msg_id="m1")
    ok = _check(ing.would_dedupe_orphan(root, "agent_message", mixed) is True,
                "mixed-key orphan after owned write -> True (fallback hash)") and ok
    return ok


# --------------------------------------------------------------------------- #
# _emit: hydration-index receipt failure is swallowed (write still succeeds)
# --------------------------------------------------------------------------- #
def test_emit_hydration_failure_swallowed() -> bool:
    ok = True
    orig = hydration_index_store.note_authoritative_append

    def _boom(*a, **k):
        raise RuntimeError("hydration boom")

    hydration_index_store.note_authoritative_append = _boom  # type: ignore[assignment]
    try:
        ing = _fresh()
        seq = ing.ingest("w-hyd", sid="sid-A", event_type="agent_message",
                         data=_agent("h1"), source="t", msg_id="m1")
        ok = _check(seq == 1, "write succeeds despite hydration receipt failure",
                    f"{seq}") and ok
        out, _, _ = ing.read_events("w-hyd")
        ok = _check(len(out) == 1, "row persisted despite hydration failure",
                    f"{len(out)}") and ok
    finally:
        hydration_index_store.note_authoritative_append = orig  # type: ignore[assignment]
    return ok


# --------------------------------------------------------------------------- #
# _emit -> _enqueue_search_projection: enqueue failure is swallowed
# --------------------------------------------------------------------------- #
def test_emit_search_projection_failure_swallowed() -> bool:
    ok = True
    orig = session_search_projection.note_event_written

    def _boom(*a, **k):
        raise RuntimeError("search boom")

    session_search_projection.note_event_written = _boom  # type: ignore[assignment]
    try:
        ing = _fresh()
        seq = ing.ingest("w-srch", sid="sid-A", event_type="agent_message",
                         data=_agent("s1"), source="t", msg_id="m1")
        ok = _check(seq == 1, "write succeeds despite search-projection failure",
                    f"{seq}") and ok
        out, _, _ = ing.read_events("w-srch")
        ok = _check(len(out) == 1, "row persisted despite search failure",
                    f"{len(out)}") and ok
    finally:
        session_search_projection.note_event_written = orig  # type: ignore[assignment]
    return ok


TESTS = [
    test_dedupe_by_uid_only,
    test_owner_aware_dedup,
    test_extract_uuid_shapes,
    test_ingest_sort_keys_exception,
    test_emit_run_id_recorded,
    test_canonical_rewrite_failure_falls_back_to_deepcopy,
    test_ingest_batch_empty,
    test_ingest_batch_mixed,
    test_cursor_paths,
    test_would_dedupe_orphan_sort_keys_exception,
    test_emit_hydration_failure_swallowed,
    test_emit_search_projection_failure_swallowed,
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
