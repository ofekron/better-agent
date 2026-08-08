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


def test_streaming_appends_produce_correct_precomputed_revision_via_real_path() -> None:
    sid = _make_session("omitted-rev-correctness")
    fired: list[dict] = []
    session_manager.add_listener(lambda s, change: fired.append(change) if s == sid else None)

    revisions = []
    for i in range(30):
        session_manager.apply_written_journal_event(
            sid, sid, "msg-1", "agent_message", _agent_data(f"e{i}", f"chunk {i}"), i + 1,
        fold_start_watermark=0,
        )
    projected = [c for c in fired if c.get("kind") == "journal_event_projected"]

    assert len(projected) == 30, (
        f"expected 30 journal_event_projected changes, got {len(projected)}"
    )
    for change in projected:
        payload = change["delta"]
        revisions.append(payload["omitted_payloads"]["events"]["revision"])
        assert PRECOMPUTED_REVISION_KEY not in payload and "events" not in payload, (
            "projected delta leaked the precomputed key or the events payload"
        )
    assert len(set(revisions)) == len(revisions), (
        f"streaming appends produced non-distinct revisions: {revisions}"
    )


def test_same_uuid_mutation_falls_back_to_correct_full_baseline() -> None:
    sid = _make_session("omitted-rev-replace")
    fired: list[dict] = []
    session_manager.add_listener(lambda s, change: fired.append(change) if s == sid else None)

    session_manager.apply_written_journal_event(
        sid, sid, "msg-1", "agent_message", _agent_data("e0", "v1"), 1,
    fold_start_watermark=0,
    )
    # Same uuid, mutated text -- apply_event replaces the slot in place
    # (a new dict object), same length. Must NOT be treated as a pure
    # append; must re-establish a correct full-hash baseline.
    session_manager.apply_written_journal_event(
        sid, sid, "msg-1", "agent_message", _agent_data("e0", "v2 mutated"), 2,
    fold_start_watermark=0,
    )
    projected = [c for c in fired if c.get("kind") == "journal_event_projected"]

    first_rev = projected[0]["delta"]["omitted_payloads"]["events"]["revision"]
    second_rev = projected[1]["delta"]["omitted_payloads"]["events"]["revision"]
    events_after = session_manager.get_ref(sid)["messages"][-1]["events"]

    assert len(projected) == 2, f"expected 2 projected changes, got {len(projected)}"
    assert first_rev != second_rev, (
        "same-uuid mutation did not produce a distinct revision"
    )
    assert second_rev == full_revision(events_after), (
        "same-uuid mutation did not re-establish a correct full-hash baseline"
    )


def test_append_after_replace_resumes_incremental_folding_correctly() -> None:
    sid = _make_session("omitted-rev-resume")
    fired: list[dict] = []
    session_manager.add_listener(lambda s, change: fired.append(change) if s == sid else None)

    session_manager.apply_written_journal_event(
        sid, sid, "msg-1", "agent_message", _agent_data("e0", "v1"), 1,
    fold_start_watermark=0,
    )
    session_manager.apply_written_journal_event(
        sid, sid, "msg-1", "agent_message", _agent_data("e0", "v2 mutated"), 2,
    fold_start_watermark=0,
    )
    session_manager.apply_written_journal_event(
        sid, sid, "msg-1", "agent_message", _agent_data("e1", "new event"), 3,
    fold_start_watermark=0,
    )
    projected = [c for c in fired if c.get("kind") == "journal_event_projected"]

    third_rev = projected[2]["delta"]["omitted_payloads"]["events"]["revision"]
    events_after = session_manager.get_ref(sid)["messages"][-1]["events"]
    expected = full_revision(events_after[:-1])
    from messages_delta_compaction import fold_revision
    expected = fold_revision(expected, events_after[-1])

    assert len(projected) == 3, f"expected 3 projected changes, got {len(projected)}"
    assert third_rev == expected, (
        "append after a replace did not fold from the freshly re-established baseline"
    )


def test_append_after_revision_cache_loss_rebuilds_full_revision() -> None:
    sid = _make_session("omitted-rev-reload")
    session_manager.apply_written_journal_event(
        sid, sid, "msg-1", "agent_message", _agent_data("e0", "first"), 1,
    fold_start_watermark=0,
    )
    msg = session_manager.get_ref(sid)["messages"][-1]
    msg.pop(PRECOMPUTED_REVISION_KEY, None)
    fired: list[dict] = []
    session_manager.add_listener(lambda s, change: fired.append(change) if s == sid else None)
    session_manager.apply_written_journal_event(
        sid, sid, "msg-1", "agent_message", _agent_data("e1", "second"), 2,
    fold_start_watermark=0,
    )
    revision = fired[-1]["delta"]["omitted_payloads"]["events"]["revision"]
    assert revision == full_revision(session_manager.get_ref(sid)["messages"][-1]["events"])


def test_deduped_written_event_invalidates_stale_revision() -> None:
    sid = _make_session("omitted-rev-live-dedup")
    session_manager.apply_written_journal_event(
        sid, sid, "msg-1", "agent_message", _agent_data("e0", "first"), 1,
    fold_start_watermark=0,
    )
    msg = session_manager.get_ref(sid)["messages"][-1]
    from orchs import ApplyEventCtx, get_strategy
    get_strategy("native").apply_event(
        app_session_id=sid,
        msg=msg,
        event={"type": "agent_message", "data": _agent_data("e1", "live")},
        ctx=ApplyEventCtx(root_id=sid),
        source_is_provider_stream=False,
        write_journal=False,
    )
    assert not session_manager.apply_written_journal_event(
        sid, sid, "msg-1", "agent_message", _agent_data("e1", "live"), 2,
    fold_start_watermark=0,
    )
    assert PRECOMPUTED_REVISION_KEY not in msg
    fired: list[dict] = []
    session_manager.add_listener(lambda s, change: fired.append(change) if s == sid else None)
    session_manager.apply_written_journal_event(
        sid, sid, "msg-1", "agent_message", _agent_data("e2", "journal"), 3,
    fold_start_watermark=0,
    )
    revision = fired[-1]["delta"]["omitted_payloads"]["events"]["revision"]
    assert revision == full_revision(msg["events"])


def test_revision_cache_is_not_persisted() -> None:
    sid = _make_session("omitted-rev-persist")
    session_manager.apply_written_journal_event(
        sid, sid, "msg-1", "agent_message", _agent_data("e0", "first"), 1,
    fold_start_watermark=0,
    )
    root = session_manager.get_ref(sid)
    msg = root["messages"][-1]
    assert PRECOMPUTED_REVISION_KEY in msg
    persisted = __import__("session_store").copy_persistable_tree(root)
    assert PRECOMPUTED_REVISION_KEY not in persisted["messages"][-1]
    assert PRECOMPUTED_REVISION_KEY in msg


def test_real_path_is_faster_than_naive_full_recompute_per_event() -> None:
    sid = _make_session("omitted-rev-perf")
    fired: list[dict] = []
    session_manager.add_listener(lambda s, change: fired.append(change) if s == sid else None)

    big_text = "x" * 5000
    n = 300

    start = time.perf_counter()
    for i in range(n):
        session_manager.apply_written_journal_event(
            sid, sid, "msg-1", "agent_message", _agent_data(f"e{i}", big_text), i + 1,
        fold_start_watermark=0,
        )
    real_elapsed = time.perf_counter() - start

    naive_events: list[object] = []
    start = time.perf_counter()
    for i in range(n):
        naive_events.append({"uuid": f"e{i}", "text": big_text})
        full_revision(naive_events)
    naive_elapsed = time.perf_counter() - start

    assert real_elapsed < naive_elapsed * 0.5, (
        f"real apply_written_journal_event path was not >2x faster than naive "
        f"full recompute per event "
        f"(real={real_elapsed:.4f}s naive={naive_elapsed:.4f}s, n={n})"
    )


def test_ownership_resolution_invalidates_stale_precomputed_revision() -> None:
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

    assert 'events_list.sort(' in body, (
        "apply_journal_ownership_resolution does not sort events in place"
    )
    assert "messages_delta_compaction.PRECOMPUTED_REVISION_KEY" in body, (
        "apply_journal_ownership_resolution does not invalidate the precomputed revision"
    )
    assert body.index("events_list.sort(") < body.index(
        "messages_delta_compaction.PRECOMPUTED_REVISION_KEY",
    ), (
        "apply_journal_ownership_resolution invalidates the precomputed revision "
        "before reordering events"
    )


def test_reconcile_from_jsonl_invalidates_stale_precomputed_revision() -> None:
    """render_tree_hydrate's cold-load/reconcile path also mutates a
    live msg's events (bulk merge, worker replay, orphan catch-up)
    outside apply_written_journal_event's bookkeeping -- locks that it
    invalidates the precomputed key whenever it actually changes a
    message's events."""
    source = Path(__file__).resolve().parent.parent.joinpath(
        "render_tree_hydrate.py",
    ).read_text(encoding="utf-8")

    assert "if changed_for_stub:" in source, (
        "reconcile path lacks a changed_for_stub change gate"
    )
    assert "messages_delta_compaction.PRECOMPUTED_REVISION_KEY" in source, (
        "reconcile path does not invalidate the precomputed revision"
    )
    assert source.index("if changed_for_stub:") < source.index(
        "messages_delta_compaction.PRECOMPUTED_REVISION_KEY",
    ) < source.index("if watch_change and changed_for_stub:"), (
        "reconcile path invalidates the precomputed revision in the wrong "
        "position relative to its change gates"
    )


def test_precomputed_key_excluded_from_snapshot_stub_filters() -> None:
    """The internal bookkeeping key must never leak into REST/disk
    snapshots -- both _copy_assistant_for_snapshot-style stub filters
    must exclude it alongside the existing "_uid_idx" exclusion."""
    source = Path(__file__).resolve().parent.parent.joinpath(
        "session_manager.py",
    ).read_text(encoding="utf-8")
    occurrences = source.count(
        'k not in ("events", "_uid_idx", "_has_final", '
        "messages_delta_compaction.PRECOMPUTED_REVISION_KEY)",
    )
    assert occurrences >= 2, (
        f"precomputed revision key is excluded from only {occurrences} "
        "snapshot stub filter(s), expected >= 2"
    )


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
    passed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            print(f"{FAIL} {test.__name__}: {exc}")
        except Exception:
            import traceback
            print(f"{FAIL} {test.__name__}: unexpected error")
            traceback.print_exc()
        else:
            print(f"{PASS} {test.__name__}")
            passed += 1
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    raise SystemExit(main())
