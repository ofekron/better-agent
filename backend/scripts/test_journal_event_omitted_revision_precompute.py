"""Regression + perf lock for the messages_delta_compaction O(n^2) fix.

Incident: production faulthandler dump pinned the main event loop stuck
inside json.dumps, reached via session_ws_broadcaster.on_change ->
compact_message_delta_payload -> _omitted_events_revision, triggered once
per streamed event (journal_event_projected). That function re-hashed the
ENTIRE accumulated events list on every call -- O(n) work called O(n)
times per message, O(n^2) over a turn -- causing an escalating 17s -> 38s
event-loop stall.

Fix: session_manager.apply_written_journal_event -- the one place with
genuine ground truth on whether a mutation is a pure append or a
same-slot replace -- precomputes the revision incrementally and stamps it
on the live msg (messages_delta_compaction.PRECOMPUTED_REVISION_KEY)
BEFORE the deep-copy dispatch that would otherwise destroy object
identity. compact_message_delta_payload trusts that precomputed value
when msg's own events are the sole contributor.

This test drives the REAL production call chain end-to-end (session
creation -> apply_written_journal_event -> add_listener capturing the
fired journal_event_projected change -> compact_message_delta_payload),
not just the lower-level helper in isolation -- closing exactly the gap
an earlier, disproven fix attempt missed (its fast path relied on object
identity that a deep copy upstream always destroyed, making it a no-op
in production despite passing unit tests).

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_journal_event_omitted_revision_precompute.py
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-journal-omitted-revision-")

from messages_delta_compaction import (  # noqa: E402
    PRECOMPUTED_REVISION_KEY,
    compact_message_delta_payload,
    full_revision,
)
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def teardown_module() -> None:
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


def _make_session(name: str) -> str:
    sess = session_manager.create(
        name=name, model="sonnet", cwd="/tmp/omitted-rev-test",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    session_manager.append_assistant_msg(
        sid, {"id": "msg-1", "role": "assistant", "content": "", "events": []},
    )
    return sid


def _agent_data(uid: str, text: str) -> dict:
    return {
        "uuid": uid,
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def test_streaming_appends_produce_correct_precomputed_revision_via_real_path() -> bool:
    sid = _make_session("omitted-rev-correctness")
    fired: list[dict] = []
    session_manager.add_listener(lambda s, change: fired.append(change) if s == sid else None)

    revisions = []
    for i in range(30):
        session_manager.apply_written_journal_event(
            sid, sid, "msg-1", "agent_message", _agent_data(f"e{i}", f"chunk {i}"), i + 1,
        )
    projected = [c for c in fired if c.get("kind") == "journal_event_projected"]

    ok = len(projected) == 30
    for change in projected:
        msg = change["msg"]
        assert msg.get(PRECOMPUTED_REVISION_KEY), "msg must carry a precomputed revision"
        payload = compact_message_delta_payload(msg)
        revisions.append(payload["omitted_payloads"]["events"]["revision"])
        ok = ok and PRECOMPUTED_REVISION_KEY not in payload

    ok = ok and len(set(revisions)) == len(revisions)
    print(
        f"{PASS if ok else FAIL} streaming appends via the real "
        "apply_written_journal_event path produce correct, distinct, "
        "non-leaking revisions",
    )
    return ok


def test_same_uuid_mutation_falls_back_to_correct_full_baseline() -> bool:
    sid = _make_session("omitted-rev-replace")
    fired: list[dict] = []
    session_manager.add_listener(lambda s, change: fired.append(change) if s == sid else None)

    session_manager.apply_written_journal_event(
        sid, sid, "msg-1", "agent_message", _agent_data("e0", "v1"), 1,
    )
    # Same uuid, mutated text -- apply_event replaces the slot in place
    # (a new dict object), same length. Must NOT be treated as a pure
    # append; must re-establish a correct full-hash baseline.
    session_manager.apply_written_journal_event(
        sid, sid, "msg-1", "agent_message", _agent_data("e0", "v2 mutated"), 2,
    )
    projected = [c for c in fired if c.get("kind") == "journal_event_projected"]

    first_rev = projected[0]["msg"][PRECOMPUTED_REVISION_KEY]
    second_rev = projected[1]["msg"][PRECOMPUTED_REVISION_KEY]
    events_after = projected[1]["msg"]["events"]

    ok = (
        len(projected) == 2
        and first_rev != second_rev
        and second_rev == full_revision(events_after)
    )
    print(
        f"{PASS if ok else FAIL} same-uuid mutation (replace) re-establishes "
        "a correct full-hash baseline, not a stale incremental fold",
    )
    return ok


def test_append_after_replace_resumes_incremental_folding_correctly() -> bool:
    sid = _make_session("omitted-rev-resume")
    fired: list[dict] = []
    session_manager.add_listener(lambda s, change: fired.append(change) if s == sid else None)

    session_manager.apply_written_journal_event(
        sid, sid, "msg-1", "agent_message", _agent_data("e0", "v1"), 1,
    )
    session_manager.apply_written_journal_event(
        sid, sid, "msg-1", "agent_message", _agent_data("e0", "v2 mutated"), 2,
    )
    session_manager.apply_written_journal_event(
        sid, sid, "msg-1", "agent_message", _agent_data("e1", "new event"), 3,
    )
    projected = [c for c in fired if c.get("kind") == "journal_event_projected"]

    third_rev = projected[2]["msg"][PRECOMPUTED_REVISION_KEY]
    events_after = projected[2]["msg"]["events"]
    expected = full_revision(events_after[:-1])
    from messages_delta_compaction import fold_revision
    expected = fold_revision(expected, events_after[-1])

    ok = len(projected) == 3 and third_rev == expected
    print(
        f"{PASS if ok else FAIL} a pure append immediately after a replace "
        "correctly folds from the freshly re-established baseline",
    )
    return ok


def test_real_path_is_faster_than_naive_full_recompute_per_event() -> bool:
    sid = _make_session("omitted-rev-perf")
    fired: list[dict] = []
    session_manager.add_listener(lambda s, change: fired.append(change) if s == sid else None)

    big_text = "x" * 5000
    n = 300

    start = time.perf_counter()
    for i in range(n):
        session_manager.apply_written_journal_event(
            sid, sid, "msg-1", "agent_message", _agent_data(f"e{i}", big_text), i + 1,
        )
    real_elapsed = time.perf_counter() - start

    naive_events: list[object] = []
    start = time.perf_counter()
    for i in range(n):
        naive_events.append({"uuid": f"e{i}", "text": big_text})
        full_revision(naive_events)
    naive_elapsed = time.perf_counter() - start

    ok = real_elapsed < naive_elapsed * 0.5
    print(
        f"{PASS if ok else FAIL} real apply_written_journal_event path is "
        f"faster than naive full recompute per event "
        f"(real={real_elapsed:.4f}s naive={naive_elapsed:.4f}s, n={n})",
    )
    return ok


def test_ownership_resolution_invalidates_stale_precomputed_revision() -> bool:
    """apply_journal_ownership_resolution reorders msg.events in place
    (same length, so apply_written_journal_event's before_len+1 append
    check on its NEXT call cannot detect the mutation on its own) --
    locks that it invalidates the precomputed key rather than leaving a
    stale one for the next append to incorrectly fold onto."""
    source = Path(__file__).resolve().parent.parent.joinpath(
        "session_manager.py",
    ).read_text(encoding="utf-8")
    start = source.index("    def apply_journal_ownership_resolution(")
    end = source.index("    def apply_written_journal_event(", start)
    body = source[start:end]

    ok = (
        'events_list.sort(' in body
        and "messages_delta_compaction.PRECOMPUTED_REVISION_KEY" in body
        and body.index("events_list.sort(")
        < body.index("messages_delta_compaction.PRECOMPUTED_REVISION_KEY")
    )
    print(
        f"{PASS if ok else FAIL} apply_journal_ownership_resolution "
        "invalidates the precomputed revision after reordering events",
    )
    return ok


def test_reconcile_from_jsonl_invalidates_stale_precomputed_revision() -> bool:
    """render_tree_hydrate's cold-load/reconcile path also mutates a
    live msg's events (bulk merge, worker replay, orphan catch-up)
    outside apply_written_journal_event's bookkeeping -- locks that it
    invalidates the precomputed key whenever it actually changes a
    message's events."""
    source = Path(__file__).resolve().parent.parent.joinpath(
        "render_tree_hydrate.py",
    ).read_text(encoding="utf-8")

    ok = (
        "if changed_for_stub:" in source
        and "messages_delta_compaction.PRECOMPUTED_REVISION_KEY" in source
        and source.index("if changed_for_stub:")
        < source.index("messages_delta_compaction.PRECOMPUTED_REVISION_KEY")
        < source.index("if watch_change and changed_for_stub:")
    )
    print(
        f"{PASS if ok else FAIL} reconcile_msg_events_from_jsonl invalidates "
        "the precomputed revision whenever it changes a message's events",
    )
    return ok


def test_precomputed_key_excluded_from_snapshot_stub_filters() -> bool:
    """The internal bookkeeping key must never leak into REST/disk
    snapshots -- both _copy_assistant_for_snapshot-style stub filters
    must exclude it alongside the existing "_uid_idx" exclusion."""
    source = Path(__file__).resolve().parent.parent.joinpath(
        "session_manager.py",
    ).read_text(encoding="utf-8")
    occurrences = source.count(
        'k not in ("events", "_uid_idx", '
        "messages_delta_compaction.PRECOMPUTED_REVISION_KEY)",
    )
    ok = occurrences >= 2
    print(
        f"{PASS if ok else FAIL} precomputed revision key is excluded from "
        f"both snapshot stub filters (found {occurrences})",
    )
    return ok


def main() -> int:
    tests = [
        test_streaming_appends_produce_correct_precomputed_revision_via_real_path,
        test_same_uuid_mutation_falls_back_to_correct_full_baseline,
        test_append_after_replace_resumes_incremental_folding_correctly,
        test_ownership_resolution_invalidates_stale_precomputed_revision,
        test_reconcile_from_jsonl_invalidates_stale_precomputed_revision,
        test_precomputed_key_excluded_from_snapshot_stub_filters,
        test_real_path_is_faster_than_naive_full_recompute_per_event,
    ]
    return 0 if all(test() for test in tests) else 1


if __name__ == "__main__":
    raise SystemExit(main())
