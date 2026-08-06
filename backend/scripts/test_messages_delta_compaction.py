from __future__ import annotations

import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-msgdelta-compact-")

from messages_delta_compaction import (  # noqa: E402
    PRECOMPUTED_REVISION_KEY,
    compact_message_delta_payload,
    fold_revision,
    full_revision,
)
from orchestrator import Coordinator  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_compacts_render_events_without_mutating_source():
    msg = {
        "id": "msg-1",
        "role": "assistant",
        "content": "done",
        "events": [{"type": "agent_message", "data": {"uuid": "e1"}}],
        "workers": [
            {
                "delegation_id": "d1",
                "worker_session_id": "w1",
                "events": [{"type": "agent_message", "data": {"uuid": "we1"}}],
                "success": True,
            },
            {"delegation_id": "d2", "worker_session_id": "w2"},
        ],
    }

    payload = compact_message_delta_payload(msg)

    assert "events" not in payload, "render events omitted from delta"
    assert isinstance(
        payload.get("omitted_payloads", {}).get("events", {}).get("revision"),
        str,
    ), "omitted events carry a string revision"
    assert payload["omitted_payloads"]["events"]["href"] == "messages/msg-1/events", "events href points at the message events route"
    assert payload["content"] == "done", "final content preserved"
    assert "events" not in payload["workers"][0], "worker render events omitted"
    assert payload["workers"][0]["success"] is True, "worker final fields preserved"
    assert payload["workers"][1]["worker_session_id"] == "w2", "passthrough worker preserved"
    assert msg["events"][0]["data"]["uuid"] == "e1", "source msg events not mutated"
    assert msg["workers"][0]["events"][0]["data"]["uuid"] == "we1", "source worker events not mutated"


def test_omitted_events_revision_changes_for_same_count_event_change():
    first = compact_message_delta_payload({
        "id": "msg-1",
        "events": [{"type": "agent_message", "data": {"uuid": "e1", "text": "one"}}],
    })
    second = compact_message_delta_payload({
        "id": "msg-1",
        "events": [{"type": "agent_message", "data": {"uuid": "e1", "text": "two"}}],
    })
    assert first["omitted_payloads"]["events"]["revision"] != second["omitted_payloads"]["events"]["revision"], "event payload revision changes when same-count events change"


def test_worker_only_omitted_events_get_revision():
    payload = compact_message_delta_payload({
        "id": "msg-1",
        "workers": [
            {
                "delegation_id": "d1",
                "worker_session_id": "w1",
                "events": [{"type": "agent_message", "data": {"uuid": "we1"}}],
            },
        ],
    })
    assert "events" not in payload["workers"][0], "worker render events omitted"
    assert isinstance(payload["omitted_payloads"]["events"]["revision"], str), "worker-only omitted events get revision"


def test_orchestrator_uses_shared_compaction_helper():
    coordinator = Coordinator.__new__(Coordinator)
    msg = {"id": "msg-1", "events": [1]}
    payload = coordinator._messages_delta_payload(msg, omit_render_events=True)
    assert payload == compact_message_delta_payload(msg), "orchestrator delegates to shared compaction helper"


def test_passthrough_when_not_compacting():
    coordinator = Coordinator.__new__(Coordinator)
    msg = {"id": "msg-1", "events": [1]}
    payload = coordinator._messages_delta_payload(msg, omit_render_events=False)
    assert payload is msg, "non-compacted messages_delta is passthrough"


def test_precomputed_revision_is_used_when_trustworthy():
    """session_manager stamps PRECOMPUTED_REVISION_KEY on msg before the
    deep-copy dispatch. When msg's own events are the sole contributor to
    omitted_events, compact_message_delta_payload must use it verbatim
    instead of recomputing, and must not leak the internal key."""
    msg = {
        "id": "msg-1",
        "events": [{"type": "agent_message", "data": {"uuid": "e1"}}],
        PRECOMPUTED_REVISION_KEY: "stamped-value-123",
    }
    payload = compact_message_delta_payload(msg)
    assert payload["omitted_payloads"]["events"]["revision"] == "stamped-value-123", "precomputed revision used verbatim"
    assert PRECOMPUTED_REVISION_KEY not in payload, "internal precomputed key not leaked to wire"


def test_precomputed_revision_ignored_when_workers_contribute():
    """If workers also contribute omitted events, the precomputed value
    (which only ever reflects msg's OWN events) must NOT be trusted —
    falling back to a full, correct recompute over the combined list."""
    msg = {
        "id": "msg-1",
        "events": [{"type": "agent_message", "data": {"uuid": "e1"}}],
        "workers": [{
            "delegation_id": "d1",
            "events": [{"type": "agent_message", "data": {"uuid": "we1"}}],
        }],
        PRECOMPUTED_REVISION_KEY: "stamped-but-must-be-ignored",
    }
    payload = compact_message_delta_payload(msg)
    assert payload["omitted_payloads"]["events"]["revision"] != "stamped-but-must-be-ignored", "precomputed revision ignored when workers also contribute events"


def test_missing_precomputed_falls_back_to_full_recompute():
    """No stamped key (e.g. a message that never went through
    apply_written_journal_event) must still get a correct revision."""
    events = [{"type": "agent_message", "data": {"uuid": "e1"}}]
    payload = compact_message_delta_payload({"id": "msg-1", "events": events})
    assert payload["omitted_payloads"]["events"]["revision"] == full_revision(events), "missing precomputed revision falls back to full recompute"


def test_empty_events_still_reported_as_omitted():
    """Stripping an EMPTY events list must still emit the omitted marker.
    The client restores its own live events only when the marker is
    present, so without it a delta silently wipes them."""
    payload = compact_message_delta_payload({
        "id": "msg-1",
        "content": "final answer",
        "events": [],
        "workers": [{"delegation_id": "d1", "events": []}],
    })
    assert "events" not in payload, "empty events stripped from delta"
    assert "events" not in payload["workers"][0], "empty worker events stripped"
    assert isinstance(
        payload.get("omitted_payloads", {}).get("events", {}).get("revision"),
        str,
    ), "empty stripped events still reported as omitted"
    assert payload["omitted_payloads"]["events"]["count"] == 0, "omitted count is 0"


def test_internal_bookkeeping_keys_never_reach_the_wire():
    """`_uid_idx` & friends are O(N) in the event count — shipping them on
    a per-event delta reinstates the O(N^2) wire cost compaction removes."""
    payload = compact_message_delta_payload({
        "id": "msg-1",
        "events": [{"type": "agent_message", "data": {"uuid": "e1"}}],
        "_uid_idx": {"e1": 0},
        "_has_final": True,
        "_content_dirty": False,
        "_events_seq_floor": 12,
        "workers": [
            {
                "delegation_id": "d1",
                "events": [{"type": "agent_message", "data": {"uuid": "we1"}}],
                "_uid_idx": {"we1": 0},
                "_has_final": False,
            },
        ],
    })
    leaked = [
        key for key in ("_uid_idx", "_has_final", "_content_dirty", "_events_seq_floor")
        if key in payload or key in payload["workers"][0]
    ]
    assert not leaked, f"internal bookkeeping keys leaked to wire: {leaked}"
    assert payload["omitted_payloads"]["events"]["count"] == 2, "omitted count is 2 (msg + worker event)"


def test_fold_revision_changes_with_each_new_event_and_is_deterministic():
    prev = full_revision([])
    a = fold_revision(prev, {"uuid": "e1", "text": "one"})
    b = fold_revision(a, {"uuid": "e2", "text": "two"})
    a_again = fold_revision(prev, {"uuid": "e1", "text": "one"})
    assert a != b, "revision changes with each new event"
    assert a != prev, "fold differs from empty base"
    assert a == a_again, "fold_revision is deterministic"


def test_non_dict_worker_entry_is_passed_through():
    """A malformed (non-dict) worker entry is forwarded verbatim instead of
    crashing the compaction loop; legitimate dict workers are unaffected."""
    payload = compact_message_delta_payload({
        "id": "msg-1",
        "workers": [
            "not-a-dict",
            {"delegation_id": "d1", "worker_session_id": "w1"},
        ],
    })
    assert payload["workers"][0] == "not-a-dict", "non-dict worker entry passed through verbatim"
    assert payload["workers"][1]["worker_session_id"] == "w1", "legitimate dict worker unaffected"


def test_no_omitted_marker_when_nothing_stripped():
    """With no `events` key on msg or any worker, events_key_removed stays
    False, so no omitted_payloads marker is emitted and the payload is the
    message minus internal bookkeeping keys."""
    payload = compact_message_delta_payload({
        "id": "msg-1",
        "content": "final",
        "workers": [{"delegation_id": "d1", "worker_session_id": "w1"}],
    })
    assert "omitted_payloads" not in payload, "no omitted marker when no events were stripped"
    assert payload["content"] == "final", "content preserved"


def test_non_list_events_value_contributes_nothing():
    """A non-list `events` value is stripped (key removed) but contributes
    nothing to omitted_events; own_events_present stays False, so the
    precomputed revision is never trusted for it."""
    payload = compact_message_delta_payload({
        "id": "msg-1",
        "events": "not-a-list",
    })
    assert "events" not in payload, "non-list events value stripped"
    assert payload["omitted_payloads"]["events"]["count"] == 0, "non-list events contribute nothing to omitted count"


def test_no_href_when_message_id_absent():
    """Without a usable message id the events ref carries only revision and
    count — never an href."""
    payload = compact_message_delta_payload({
        "events": [{"type": "agent_message", "data": {"uuid": "e1"}}],
    })
    ref = payload["omitted_payloads"]["events"]
    assert "href" not in ref, "events ref omits href when message id is absent"
    assert isinstance(ref["revision"], str), "revision still present"
    assert ref["count"] == 1, "count is 1"


TESTS = [
    test_compacts_render_events_without_mutating_source,
    test_omitted_events_revision_changes_for_same_count_event_change,
    test_worker_only_omitted_events_get_revision,
    test_orchestrator_uses_shared_compaction_helper,
    test_passthrough_when_not_compacting,
    test_precomputed_revision_is_used_when_trustworthy,
    test_precomputed_revision_ignored_when_workers_contribute,
    test_missing_precomputed_falls_back_to_full_recompute,
    test_empty_events_still_reported_as_omitted,
    test_internal_bookkeeping_keys_never_reach_the_wire,
    test_non_dict_worker_entry_is_passed_through,
    test_no_omitted_marker_when_nothing_stripped,
    test_non_list_events_value_contributes_nothing,
    test_no_href_when_message_id_absent,
    test_fold_revision_changes_with_each_new_event_and_is_deterministic,
]


def main() -> None:
    failed = 0
    total = len(TESTS)
    try:
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
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
