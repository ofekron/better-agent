"""Regression tests for the worker-panel payload/scan fixes.

Pins the contract that the heavy read paths never ship or scan legacy
worker events eagerly:

  1. A COMPLETED assistant message with a worker panel whose events
     live in events.jsonl ships `workers[*].events == []` in the
     stubbed tree snapshot (the 41MB-session bug: panels used to be
     rehydrated with their FULL journal event lists on every GET).
  2. A STREAMING assistant message still gets journal worker events
     routed into its panel (live rendering unaffected).
  3. `_legacy_worker_events_by_delegation` returns the panel's journal
     events via the incremental byte index — the lazy-expand hydrate
     path still sees every legacy row, including msg_id-less ones.
  4. Building the stubbed tree performs ZERO full-journal
     `read_events` scans (it used to full-parse events.jsonl once per
     node with panels).
  5. Journal growth after a warm summaries state does NOT trigger a
     full re-parse: `worker_event_rows` picks up appended rows through
     the incremental append fold alone.

Run with:
    cd backend && .venv/bin/python scripts/test_worker_events_stub_payload.py
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-worker-stub-")

import _test_installation  # noqa: E402
from pathlib import Path  # noqa: E402
_test_installation.activate(Path(_TMP_HOME))

from event_ingester import event_ingester  # noqa: E402
from event_journal import event_journal_reader  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _worker_event_data(delegation_id: str, uuid: str, text: str) -> dict:
    return {
        "delegation_id": delegation_id,
        "event": {
            "type": "agent_message",
            "data": {
                "uuid": uuid,
                "type": "assistant",
                "message": {"content": text},
            },
        },
    }


def _mk_session_with_panel(
    *, streaming: bool, delegation_id: str, n_events: int = 3,
    msg_id_stamped: bool = True,
) -> tuple[str, str]:
    """Create a session whose assistant msg has a worker panel and
    `n_events` worker_event rows in the journal. Returns (sid, msg_id)."""
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp",
        orchestration_mode="manager", source="cli",
    )
    sid = sess["id"]
    msg = {
        "id": f"msg-{delegation_id}",
        "role": "assistant",
        "events": [],
        "isStreaming": streaming,
        "workers": [{
            "delegation_id": delegation_id,
            "panel_kind": "worker",
            "events": [],
        }],
    }
    session_manager.append_assistant_msg(sid, msg)
    for i in range(n_events):
        event_ingester.ingest(
            sid, sid, "worker_event",
            _worker_event_data(delegation_id, f"{delegation_id}-u{i}", f"w{i}"),
            source="test",
            msg_id=msg["id"] if msg_id_stamped else None,
        )
    return sid, msg["id"]


def _tree_assistant(tree: dict, msg_id: str) -> dict:
    return next(m for m in tree["messages"] if m.get("id") == msg_id)


def test_completed_message_ships_empty_worker_events() -> bool:
    sid, msg_id = _mk_session_with_panel(
        streaming=False, delegation_id="d-completed",
    )
    tree = session_manager.get_root_tree_stubbed(sid, exchange_count=3)
    asst = _tree_assistant(tree, msg_id)
    workers = asst.get("workers") or []
    stub = asst.get("stub") or {}
    return (
        len(workers) == 1
        and workers[0].get("events") == []
        and asst.get("events") == []
        # msg_id-stamped worker rows still reach the collapsed count
        # via the journal summaries even though no events ship.
        and stub.get("event_count") == 3
    )


def test_streaming_message_keeps_journal_worker_events() -> bool:
    sid, msg_id = _mk_session_with_panel(
        streaming=True, delegation_id="d-streaming",
    )
    tree = session_manager.get_root_tree_stubbed(sid, exchange_count=3)
    asst = _tree_assistant(tree, msg_id)
    workers = asst.get("workers") or []
    return len(workers) == 1 and len(workers[0].get("events") or []) == 3


def test_legacy_hydrate_returns_all_rows_including_msg_id_less() -> bool:
    sid, _ = _mk_session_with_panel(
        streaming=False, delegation_id="d-legacy", msg_id_stamped=False,
    )
    by_delegation = session_manager._legacy_worker_events_by_delegation(
        sid, sid, {"d-legacy"},
    )
    events = by_delegation.get("d-legacy") or []
    return len(events) == 3


def test_stubbed_tree_build_does_zero_full_journal_scans() -> bool:
    sid, _ = _mk_session_with_panel(
        streaming=False, delegation_id="d-scan",
    )
    calls = []
    real_reader = event_journal_reader.read_events
    real_ingester = event_ingester.read_events

    def _counting_reader(*args, **kwargs):
        calls.append((args, kwargs))
        return real_reader(*args, **kwargs)

    def _counting_ingester(*args, **kwargs):
        calls.append((args, kwargs))
        return real_ingester(*args, **kwargs)

    event_journal_reader.read_events = _counting_reader
    event_ingester.read_events = _counting_ingester
    try:
        session_manager.get_root_tree_stubbed(sid, exchange_count=3)
    finally:
        event_journal_reader.read_events = real_reader
        event_ingester.read_events = real_ingester
    return len(calls) == 0


def test_journal_growth_appends_without_full_reparse() -> bool:
    sid, msg_id = _mk_session_with_panel(
        streaming=False, delegation_id="d-grow", n_events=2,
    )
    # Warm the summaries/worker-row state.
    warm = event_ingester.worker_event_rows(sid, {"d-grow"})
    if len(warm.get("d-grow") or []) != 2:
        return False

    scans = []
    real_scan = event_ingester._scan_summaries

    def _counting_scan(*args, **kwargs):
        scans.append(args)
        return real_scan(*args, **kwargs)

    event_ingester._scan_summaries = _counting_scan
    try:
        event_ingester.ingest(
            sid, sid, "worker_event",
            _worker_event_data("d-grow", "d-grow-u9", "late"),
            source="test", msg_id=msg_id,
        )
        grown = event_ingester.worker_event_rows(sid, {"d-grow"})
    finally:
        event_ingester._scan_summaries = real_scan
    return len(grown.get("d-grow") or []) == 3 and len(scans) == 0


TESTS = [
    test_completed_message_ships_empty_worker_events,
    test_streaming_message_keeps_journal_worker_events,
    test_legacy_hydrate_returns_all_rows_including_msg_id_less,
    test_stubbed_tree_build_does_zero_full_journal_scans,
    test_journal_growth_appends_without_full_reparse,
]


def main() -> int:
    failures = 0
    for test in TESTS:
        try:
            ok = test()
        except Exception as exc:  # noqa: BLE001
            print(f"{FAIL} {test.__name__}: {type(exc).__name__}: {exc}")
            failures += 1
            continue
        print(f"{PASS if ok else FAIL} {test.__name__}")
        if not ok:
            failures += 1
    import shutil
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
