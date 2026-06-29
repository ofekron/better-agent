"""Proves the root cause of the duplicate-render bug: `cursor()` caches
`_seq[root_id]` from a cheap line-count scan WITHOUT seeding the dedup
sets (`_seen_event_owners` / `_seen_uuids`). `_ensure_open` then
early-returns on `_seq` alone, so the first ingest after a `cursor()`
call (every subscribed session — `add_subscriber` runs `cursor()`)
skips the disk seed and leaves dedup empty. The dual writers (SDK
callback `apply_event` + jsonl tailer `ingest_orphan`) then both write
the same event → duplicate rows in events.jsonl → duplicate rendered
content.

Fails on pre-fix code (the re-ingest writes a second row, seq=2) and
passes once `_ensure_open` gates its early-return on the dedup set
being seeded too (so a `cursor()`-poisoned `_seq` still triggers a
proper seed and the re-ingest is deduped to -1).

Run with:
    cd backend && .venv/bin/python scripts/test_event_ingester_cursor_seeds_dedup.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-ingester-cursor-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_ingester import EventIngester  # noqa: E402
from paths import ba_home  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

ROOT = "root-cursor-dedup-test"
SID = "sid-cursor-dedup-test"
DATA = {
    "uuid": "u-cursor-dedup",
    "type": "assistant",
    "message": {"content": [{"type": "text", "text": "hi"}]},
}


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []

    # Prior run writes one event (seeds its own dedup, then is discarded).
    EventIngester().ingest(
        ROOT, sid=SID, event_type="agent_message",
        data=DATA, source="prior-run", msg_id="msg-1",
    )

    # Fresh instance = backend restart: dedup sets start empty.
    ing = EventIngester()

    # `cursor()` is the first touch — it caches `_seq[ROOT]` from a line
    # count WITHOUT seeding dedup. This is the bug condition.
    cursor = ing.cursor(ROOT)
    results.append((
        "cursor() returns the on-disk line count",
        cursor == 1,
        f"cursor={cursor}",
    ))

    seq_before = ing._seq.get(ROOT)
    owners_seeded_before = ROOT in ing._seen_event_owners
    results.append((
        "cursor() poisoned _seq but did NOT seed dedup",
        seq_before == 1 and not owners_seeded_before,
        f"_seq={seq_before} _seen_event_owners_seeded={owners_seeded_before}",
    ))

    # Re-ingest the SAME event the dual-writer path would re-emit.
    seq = ing.ingest(
        ROOT, sid=SID, event_type="agent_message",
        data=DATA, source="live-callback", msg_id="msg-1",
        cwd_override="",
    )
    results.append((
        "re-ingest of an on-disk event after cursor() is deduped (-1)",
        seq == -1,
        f"seq={seq} (a new seq here means a duplicate row was written)",
    ))

    # The on-disk file must still hold exactly one row.
    events_path = ba_home() / "sessions" / ROOT / "events.jsonl"
    n_rows = sum(
        1 for line in events_path.read_text().splitlines() if line.strip()
    )
    results.append((
        "events.jsonl has exactly one row (no duplicate written)",
        n_rows == 1,
        f"rows={n_rows}",
    ))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' — ' + msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def _run_max_seq_seeds_cursor() -> bool:
    root = "root-max-seq-cursor-test"
    sid = "sid-max-seq-cursor-test"
    EventIngester().ingest(
        root, sid=sid, event_type="agent_message",
        data={**DATA, "uuid": "u-max-seq-cursor-1"},
        source="prior-run", msg_id="msg-1",
    )
    EventIngester().ingest(
        root, sid=sid, event_type="agent_message",
        data={**DATA, "uuid": "u-max-seq-cursor-2"},
        source="prior-run", msg_id="msg-2",
    )

    ing = EventIngester()
    max_by_sid = ing.max_seq_by_sid(root)
    seq_after_scan = ing._seq.get(root)
    cursor = ing.cursor(root)
    ok = max_by_sid.get(sid) == 2 and seq_after_scan == 2 and cursor == 2
    print(
        f"  {PASS if ok else FAIL} max_seq_by_sid seeds cursor count"
        f"{'' if ok else f' — max={max_by_sid} _seq={seq_after_scan} cursor={cursor}'}"
    )
    return ok


def _run_session_event_meta_seeds_cursor() -> bool:
    root = "root-session-meta-cursor-test"
    sid = "sid-session-meta-cursor-test"
    EventIngester().ingest(
        root, sid=sid, event_type="agent_message",
        data={**DATA, "uuid": "u-session-meta-cursor-1"},
        source="prior-run", msg_id="msg-1",
    )
    EventIngester().ingest(
        root, sid=sid, event_type="agent_message",
        data={**DATA, "uuid": "u-session-meta-cursor-2"},
        source="prior-run", msg_id="msg-2",
    )

    ing = EventIngester()
    has_events, cursor, render_by_sid = ing.session_event_meta(root)
    seq_after_scan = ing._seq.get(root)
    ok = has_events and cursor == 2 and seq_after_scan == 2 and render_by_sid.get(sid) == 2
    print(
        f"  {PASS if ok else FAIL} session_event_meta seeds cursor and render watermarks"
        f"{'' if ok else f' — has={has_events} cursor={cursor} _seq={seq_after_scan} render={render_by_sid}'}"
    )
    return ok


def _run_session_event_meta_uses_valid_sidecar() -> bool:
    root = "root-session-meta-sidecar-test"
    sid = "sid-session-meta-sidecar-test"
    ing = EventIngester()
    ing.ingest(
        root, sid=sid, event_type="agent_message",
        data={**DATA, "uuid": "u-session-meta-sidecar"},
        source="prior-run", msg_id="msg-1",
    )
    events_path = ba_home() / "sessions" / root / "events.jsonl"
    stat = events_path.stat()
    sidecar_path = ba_home() / "sessions" / root / "event_meta.json"
    sidecar_path.write_text(
        json.dumps({
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "seq": 77,
            "max_seq_by_sid": {sid: 88},
            "render_seq_by_sid": {sid: 99},
            "root_events_version": 3,
            "root_events_candidate_version": 2,
            "root_events_by_sid": {sid: [{"type": "agent_message", "data": {"ok": True}}]},
        }),
        encoding="utf-8",
    )

    fresh = EventIngester()
    has_events, cursor, render_by_sid = fresh.session_event_meta(root)
    ok = (
        has_events
        and cursor == 77
        and render_by_sid == {sid: 99}
        and fresh._max_seq_by_sid.get(root) == {sid: 88}
        and fresh._root_events_version.get(root) == 3
        and fresh._root_events_candidate_version.get(root) == 2
        and fresh.root_events_by_sid(root) == {sid: [{"type": "agent_message", "data": {"ok": True}}]}
    )
    print(
        f"  {PASS if ok else FAIL} session_event_meta uses valid event-meta sidecar"
        f"{'' if ok else f' — cursor={cursor} render={render_by_sid} max={fresh._max_seq_by_sid.get(root)}'}"
    )
    return ok


def _run_session_event_meta_ignores_stale_sidecar() -> bool:
    root = "root-session-meta-stale-sidecar-test"
    sid = "sid-session-meta-stale-sidecar-test"
    ing = EventIngester()
    ing.ingest(
        root, sid=sid, event_type="agent_message",
        data={**DATA, "uuid": "u-session-meta-stale-sidecar-1"},
        source="prior-run", msg_id="msg-1",
    )
    events_path = ba_home() / "sessions" / root / "events.jsonl"
    stat = events_path.stat()
    sidecar_path = ba_home() / "sessions" / root / "event_meta.json"
    sidecar_path.write_text(
        json.dumps({
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "seq": 77,
            "max_seq_by_sid": {sid: 88},
            "render_seq_by_sid": {sid: 99},
            "root_events_version": 3,
            "root_events_candidate_version": 2,
        }),
        encoding="utf-8",
    )
    ing.ingest(
        root, sid=sid, event_type="agent_message",
        data={**DATA, "uuid": "u-session-meta-stale-sidecar-2"},
        source="prior-run", msg_id="msg-2",
    )

    fresh = EventIngester()
    has_events, cursor, render_by_sid = fresh.session_event_meta(root)
    ok = has_events and cursor == 2 and render_by_sid == {sid: 2}
    print(
        f"  {PASS if ok else FAIL} session_event_meta ignores stale event-meta sidecar"
        f"{'' if ok else f' — cursor={cursor} render={render_by_sid}'}"
    )
    return ok


def _run_message_summaries_uses_valid_sidecar() -> bool:
    root = "root-message-summary-sidecar-test"
    sid = "sid-message-summary-sidecar-test"
    ing = EventIngester()
    ing.ingest(
        root, sid=sid, event_type="agent_message",
        data={**DATA, "uuid": "u-message-summary-sidecar"},
        source="prior-run", msg_id="msg-1",
    )
    events_path = ba_home() / "sessions" / root / "events.jsonl"
    stat = events_path.stat()
    sidecar_path = ba_home() / "sessions" / root / "event_summaries.json"
    expected_summary = {
        "sid": sid,
        "event_count": 123,
        "last_events": [{"seq": 9, "type": "agent_message", "data": {"ok": True}}],
        "seq_start": 9,
        "seq_end": 9,
        "byte_start": 1,
        "byte_end": 2,
    }
    sidecar_path.write_text(
        json.dumps({
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "tail": 25,
            "summaries": {"msg-1": expected_summary},
            "resolutions": {"9": "msg-1"},
        }),
        encoding="utf-8",
    )

    fresh = EventIngester()
    summaries = fresh.message_event_summaries(root, tail=25)
    ok = summaries == {"msg-1": expected_summary}
    print(
        f"  {PASS if ok else FAIL} message_event_summaries uses valid sidecar"
        f"{'' if ok else f' — summaries={summaries}'}"
    )
    return ok


def _run_message_summaries_ignores_stale_sidecar() -> bool:
    root = "root-message-summary-stale-sidecar-test"
    sid = "sid-message-summary-stale-sidecar-test"
    ing = EventIngester()
    ing.ingest(
        root, sid=sid, event_type="agent_message",
        data={**DATA, "uuid": "u-message-summary-stale-sidecar-1"},
        source="prior-run", msg_id="msg-1",
    )
    events_path = ba_home() / "sessions" / root / "events.jsonl"
    stat = events_path.stat()
    sidecar_path = ba_home() / "sessions" / root / "event_summaries.json"
    sidecar_path.write_text(
        json.dumps({
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "tail": 25,
            "summaries": {"msg-1": {"sid": sid, "event_count": 99}},
            "resolutions": {},
        }),
        encoding="utf-8",
    )
    ing.ingest(
        root, sid=sid, event_type="agent_message",
        data={**DATA, "uuid": "u-message-summary-stale-sidecar-2"},
        source="prior-run", msg_id="msg-1",
    )

    fresh = EventIngester()
    summaries = fresh.message_event_summaries(root, tail=25)
    summary = summaries.get("msg-1") or {}
    ok = summary.get("event_count") == 2
    print(
        f"  {PASS if ok else FAIL} message_event_summaries ignores stale sidecar"
        f"{'' if ok else f' — summary={summary}'}"
    )
    return ok


def main() -> int:
    try:
        ok = _run()
        ok = _run_max_seq_seeds_cursor() and ok
        ok = _run_session_event_meta_seeds_cursor() and ok
        ok = _run_session_event_meta_uses_valid_sidecar() and ok
        ok = _run_session_event_meta_ignores_stale_sidecar() and ok
        ok = _run_message_summaries_uses_valid_sidecar() and ok
        ok = _run_message_summaries_ignores_stale_sidecar() and ok
        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
