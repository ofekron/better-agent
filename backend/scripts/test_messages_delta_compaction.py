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


def test_compacts_render_events_without_mutating_source() -> bool:
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

    ok = (
        "events" not in payload
        and isinstance(
            payload.get("omitted_payloads", {}).get("events", {}).get("revision"),
            str,
        )
        and payload["omitted_payloads"]["events"]["href"] == "messages/msg-1/events"
        and payload["content"] == "done"
        and "events" not in payload["workers"][0]
        and payload["workers"][0]["success"] is True
        and payload["workers"][1]["worker_session_id"] == "w2"
        and msg["events"][0]["data"]["uuid"] == "e1"
        and msg["workers"][0]["events"][0]["data"]["uuid"] == "we1"
    )
    print(
        f"{PASS if ok else FAIL} compact messages_delta omits render events "
        "while preserving final fields",
    )
    return ok


def test_omitted_events_revision_changes_for_same_count_event_change() -> bool:
    first = compact_message_delta_payload({
        "id": "msg-1",
        "events": [{"type": "agent_message", "data": {"uuid": "e1", "text": "one"}}],
    })
    second = compact_message_delta_payload({
        "id": "msg-1",
        "events": [{"type": "agent_message", "data": {"uuid": "e1", "text": "two"}}],
    })

    ok = (
        first["omitted_payloads"]["events"]["revision"]
        != second["omitted_payloads"]["events"]["revision"]
    )
    print(
        f"{PASS if ok else FAIL} event payload revision changes when same-count "
        "events change",
    )
    return ok


def test_worker_only_omitted_events_get_revision() -> bool:
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

    ok = (
        "events" not in payload["workers"][0]
        and isinstance(payload["omitted_payloads"]["events"]["revision"], str)
    )
    print(f"{PASS if ok else FAIL} worker-only omitted events get revision")
    return ok


def test_orchestrator_uses_shared_compaction_helper() -> bool:
    coordinator = Coordinator.__new__(Coordinator)
    msg = {"id": "msg-1", "events": [1]}
    payload = coordinator._messages_delta_payload(
        msg,
        omit_render_events=True,
    )
    ok = payload == compact_message_delta_payload(msg)
    print(f"{PASS if ok else FAIL} orchestrator uses shared compaction helper")
    return ok


def test_passthrough_when_not_compacting() -> bool:
    coordinator = Coordinator.__new__(Coordinator)
    msg = {"id": "msg-1", "events": [1]}
    payload = coordinator._messages_delta_payload(
        msg,
        omit_render_events=False,
    )
    ok = payload is msg
    print(f"{PASS if ok else FAIL} non-compacted messages_delta is passthrough")
    return ok


def test_precomputed_revision_is_used_when_trustworthy() -> bool:
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

    ok = (
        payload["omitted_payloads"]["events"]["revision"] == "stamped-value-123"
        and PRECOMPUTED_REVISION_KEY not in payload
    )
    print(
        f"{PASS if ok else FAIL} precomputed revision is used verbatim and "
        "not leaked to the outgoing payload",
    )
    return ok


def test_precomputed_revision_ignored_when_workers_contribute() -> bool:
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

    ok = payload["omitted_payloads"]["events"]["revision"] != "stamped-but-must-be-ignored"
    print(
        f"{PASS if ok else FAIL} precomputed revision is ignored when "
        "workers also contribute events",
    )
    return ok


def test_missing_precomputed_falls_back_to_full_recompute() -> bool:
    """No stamped key (e.g. a message that never went through
    apply_written_journal_event) must still get a correct revision."""
    events = [{"type": "agent_message", "data": {"uuid": "e1"}}]
    payload = compact_message_delta_payload({"id": "msg-1", "events": events})

    ok = payload["omitted_payloads"]["events"]["revision"] == full_revision(events)
    print(
        f"{PASS if ok else FAIL} missing precomputed revision falls back "
        "to full recompute",
    )
    return ok


def test_empty_events_still_reported_as_omitted() -> bool:
    """Stripping an EMPTY events list must still emit the omitted marker.
    The client restores its own live events only when the marker is
    present, so without it a delta silently wipes them."""
    payload = compact_message_delta_payload({
        "id": "msg-1",
        "content": "final answer",
        "events": [],
        "workers": [{"delegation_id": "d1", "events": []}],
    })

    ok = (
        "events" not in payload
        and "events" not in payload["workers"][0]
        and isinstance(
            payload.get("omitted_payloads", {}).get("events", {}).get("revision"),
            str,
        )
        and payload["omitted_payloads"]["events"]["count"] == 0
    )
    print(f"{PASS if ok else FAIL} empty stripped events still reported as omitted")
    return ok


def test_internal_bookkeeping_keys_never_reach_the_wire() -> bool:
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
    ok = not leaked and payload["omitted_payloads"]["events"]["count"] == 2
    print(
        f"{PASS if ok else FAIL} internal bookkeeping keys never reach the wire"
        + (f" (leaked: {leaked})" if leaked else ""),
    )
    return ok


def test_fold_revision_changes_with_each_new_event_and_is_deterministic() -> bool:
    prev = full_revision([])
    a = fold_revision(prev, {"uuid": "e1", "text": "one"})
    b = fold_revision(a, {"uuid": "e2", "text": "two"})
    a_again = fold_revision(prev, {"uuid": "e1", "text": "one"})

    ok = a != b and a != prev and a == a_again
    print(
        f"{PASS if ok else FAIL} fold_revision is deterministic and "
        "changes with each new event",
    )
    return ok


def test_non_dict_worker_entry_is_passed_through() -> bool:
    """A malformed (non-dict) worker entry is forwarded verbatim instead of
    crashing the compaction loop; legitimate dict workers are unaffected."""
    payload = compact_message_delta_payload({
        "id": "msg-1",
        "workers": [
            "not-a-dict",
            {"delegation_id": "d1", "worker_session_id": "w1"},
        ],
    })

    ok = (
        payload["workers"][0] == "not-a-dict"
        and payload["workers"][1]["worker_session_id"] == "w1"
    )
    print(
        f"{PASS if ok else FAIL} non-dict worker entry passed through verbatim",
    )
    return ok


def test_no_omitted_marker_when_nothing_stripped() -> bool:
    """With no `events` key on msg or any worker, events_key_removed stays
    False, so no omitted_payloads marker is emitted and the payload is the
    message minus internal bookkeeping keys."""
    payload = compact_message_delta_payload({
        "id": "msg-1",
        "content": "final",
        "workers": [{"delegation_id": "d1", "worker_session_id": "w1"}],
    })

    ok = "omitted_payloads" not in payload and payload["content"] == "final"
    print(
        f"{PASS if ok else FAIL} no omitted marker when no events were stripped",
    )
    return ok


def test_non_list_events_value_contributes_nothing() -> bool:
    """A non-list `events` value is stripped (key removed) but contributes
    nothing to omitted_events; own_events_present stays False, so the
    precomputed revision is never trusted for it."""
    payload = compact_message_delta_payload({
        "id": "msg-1",
        "events": "not-a-list",
    })

    ok = (
        "events" not in payload
        and payload["omitted_payloads"]["events"]["count"] == 0
    )
    print(
        f"{PASS if ok else FAIL} non-list events value stripped but not collected",
    )
    return ok


def test_no_href_when_message_id_absent() -> bool:
    """Without a usable message id the events ref carries only revision and
    count — never an href."""
    payload = compact_message_delta_payload({
        "events": [{"type": "agent_message", "data": {"uuid": "e1"}}],
    })

    ref = payload["omitted_payloads"]["events"]
    ok = "href" not in ref and isinstance(ref["revision"], str) and ref["count"] == 1
    print(
        f"{PASS if ok else FAIL} events ref omits href when message id is absent",
    )
    return ok


def main() -> int:
    try:
        tests = [
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
        return 0 if all(test() for test in tests) else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
