"""Regression test for the mirror_event/ensure_cutover raw-vs-rewritten
canonicalization divergence.

Root cause: `EventJournalWriter._append_resolved` used to pass the
caller's RAW `event.data` (pre file-ref rewrite) into
`canonical_runtime_journal().mirror_event(...)`, while
`ensure_cutover`'s gap-fill re-derivation reads back the REWRITTEN bytes
that `event_ingester` actually persisted to `events.jsonl`. For an event
whose content contains a rewritable path (e.g. a tool_result mentioning
a file), the two derivations disagreed on `content_hash` for the same
`(root_id, root_generation, source_stream_id, source_event_id,
source_generation, source_sequence)` identity key, and a later
`canonical_event_store.SourceConflictError` on cutover was permanent --
`journal_through_seq` never advances on failure, so every retry
re-derives and re-hits the identical mismatch forever.

Note this can't be reproduced as a live end-to-end mirror_event-then-
gap-fill integration test within a single test run: a successful
`mirror_event` commit always advances `journal_through_seq` to cover
its own seq, so `ensure_cutover`'s `_validated_gap_rows` structurally
skips re-deriving that same seq afterward (by design -- that's what
makes gap-fill incremental). The actual production corruption came from
mirroring under the OLD buggy code at some point in a session's history,
followed by an authority reset that made a LATER full `ensure_cutover`
re-derive everything from scratch and collide with the stale,
wrongly-canonicalized fact still sitting in the store.

So this test verifies the fix at its actual call site
(`EventJournalWriter._append_resolved`) two ways: (1) intercept what
`mirror_event` is actually called with and confirm it's the rewritten
bytes, not raw `event.data`; (2) confirm content_hash derived from that
intercepted payload matches content_hash derived from the row read back
off disk (what a gap-fill sees), and that it would NOT have matched had
the raw payload been used instead -- proving this is a genuine
regression test, not a tautology.
"""
from __future__ import annotations

import os
import shutil
import sys
import uuid
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-mirror-cutover-rewrite-")

import event_ingester as event_ingester_module  # noqa: E402
from event_ingester import event_ingester as ing  # noqa: E402
from event_journal import (  # noqa: E402
    Event,
    EventJournalWriter,
    MessageOwnership,
    ResolvedEvent,
)
from canonical_event_adapter import canonical_facts_from_rows  # noqa: E402
from canonical_runtime_journal import (  # noqa: E402
    CanonicalRuntimeJournal,
    canonical_runtime_journal,
)

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

_REWRITABLE_TEXT = "Success. Updated the following files:\nA backend/new_file.py\n"


def _tool_result_data(uid: str) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "call_1",
                "content": _REWRITABLE_TEXT,
            }],
        },
        "uuid": uid,
    }


def _with_ref_ctx(repo: Path, assume_exists: bool):
    original = event_ingester_module._ref_ctx_for_root
    event_ingester_module._ref_ctx_for_root = lambda _root_id: (str(repo), assume_exists)
    return original


def _restore_ref_ctx(original) -> None:
    event_ingester_module._ref_ctx_for_root = original


def _content_hash_for(root_id: str, seq: int, sid: str, msg_id: str, data: dict) -> str:
    row = {
        "root_id": root_id, "root_generation": 0, "sid": sid, "seq": seq,
        "type": "agent_message", "data": data, "source": "test", "msg_id": msg_id,
    }
    facts = canonical_facts_from_rows([row])
    matches = [f for f in facts if f.source_order.sequence == seq]
    assert matches, f"no fact derived for seq={seq}"
    return matches[0].content_hash


def test_append_resolved_mirrors_rewritten_data() -> bool:
    """Call the REAL `_append_resolved` (the fixed call site) with a
    rewritable tool_result and intercept what it actually passes to
    `mirror_event`. Must be the rewritten bytes persisted to disk, not
    the caller's raw `event.data`."""
    root_id = sid = str(uuid.uuid4())
    msg_id = str(uuid.uuid4())
    uid = str(uuid.uuid4())
    repo = Path(_TMP_HOME) / "repo"
    raw_data = _tool_result_data(uid)

    captured: list[dict] = []
    original_mirror_event = CanonicalRuntimeJournal.mirror_event

    def _spy_mirror_event(self, **kwargs):
        captured.append(kwargs.get("data"))
        return original_mirror_event(self, **kwargs)

    CanonicalRuntimeJournal.mirror_event = _spy_mirror_event
    writer = EventJournalWriter()
    original_ref_ctx = _with_ref_ctx(repo, True)
    try:
        resolved = ResolvedEvent(
            root_id=root_id, sid=sid, event_type="agent_message",
            data=raw_data, source="test",
            ownership=MessageOwnership(msg_id), event_id=uid,
        )
        written = writer._append_resolved(resolved)
    finally:
        _restore_ref_ctx(original_ref_ctx)
        CanonicalRuntimeJournal.mirror_event = original_mirror_event
        writer.close()

    disk_rows, _, _ = ing.read_events(root_id, after_seq=0, limit=100)
    ing.close_all()
    ok = (
        written.seq >= 0
        and len(captured) == 1
        and captured[0] is not None
        and captured[0] != raw_data
        and disk_rows
        and disk_rows[0]["data"] == captured[0]
    )
    if not ok:
        print(
            f"  seq={written.seq} captured={captured} raw_data={raw_data!r} "
            f"disk_row_data={disk_rows[0]['data'] if disk_rows else None!r}",
        )
    return ok


def test_mirrored_and_disk_content_hash_agree_and_would_not_have_pre_fix() -> bool:
    """The invariant the fix restores: content_hash derived from what
    `_append_resolved` mirrors equals content_hash derived from the row
    `ensure_cutover`'s gap-fill reads back off disk -- and, for contrast,
    the OLD behavior (raw `event.data`) would NOT have matched, i.e.
    pre-fix this would have raised `SourceConflictError` the moment both
    were ever compared for the same identity key."""
    root_id = sid = str(uuid.uuid4())
    msg_id = str(uuid.uuid4())
    uid = str(uuid.uuid4())
    repo = Path(_TMP_HOME) / "repo"
    raw_data = _tool_result_data(uid)

    captured: list[dict] = []
    original_mirror_event = CanonicalRuntimeJournal.mirror_event

    def _spy_mirror_event(self, **kwargs):
        captured.append(kwargs.get("data"))
        return original_mirror_event(self, **kwargs)

    CanonicalRuntimeJournal.mirror_event = _spy_mirror_event
    writer = EventJournalWriter()
    original_ref_ctx = _with_ref_ctx(repo, True)
    try:
        resolved = ResolvedEvent(
            root_id=root_id, sid=sid, event_type="agent_message",
            data=raw_data, source="test",
            ownership=MessageOwnership(msg_id), event_id=uid,
        )
        written = writer._append_resolved(resolved)
    finally:
        _restore_ref_ctx(original_ref_ctx)
        CanonicalRuntimeJournal.mirror_event = original_mirror_event
        writer.close()

    disk_rows, _, _ = ing.read_events(root_id, after_seq=0, limit=100)
    ing.close_all()
    assert disk_rows and disk_rows[0]["seq"] == written.seq
    assert captured and captured[0] is not None

    mirrored_hash = _content_hash_for(root_id, written.seq, sid, msg_id, captured[0])
    disk_hash = _content_hash_for(root_id, written.seq, sid, msg_id, disk_rows[0]["data"])
    raw_hash = _content_hash_for(root_id, written.seq, sid, msg_id, raw_data)

    ok = mirrored_hash == disk_hash and mirrored_hash != raw_hash
    if not ok:
        print(
            f"  mirrored_hash={mirrored_hash} disk_hash={disk_hash} "
            f"raw_hash={raw_hash} (expected mirrored==disk, mirrored!=raw)",
        )
    return ok


def _tool_result_data_no_uuid() -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "call_2",
                "content": _REWRITABLE_TEXT,
            }],
        },
    }


def test_catalog_reset_rederivation_is_identity_stable() -> bool:
    """The authentic production corruption scenario, end to end: mirror
    facts live under sqlite authority, then reset the authority catalog
    (as the v2->v3 catalog wipe did) while the fact store survives, and
    force `ensure_cutover` to re-derive everything from the disk rows.

    Pre-fix this forked fact identity between the two derivations --
    the mirror row lacked `run_id` (different `source_stream_id`),
    injected `event_id` as a payload uuid the disk row never carried
    (different `source_event_id` AND different content_hash for events
    without a top-level uuid), and stamped a `turn_id` the disk row
    dropped -- so a post-reset re-derivation either duplicated every
    fact under a second identity or raised SourceConflictError.
    Post-fix the re-derivation must be a byte-stable no-op: same fact
    count, same identities, same content hashes, same turn_ids."""
    root_id = sid = str(uuid.uuid4())
    msg_id = str(uuid.uuid4())
    uid = str(uuid.uuid4())
    repo = Path(_TMP_HOME) / "repo"

    journal = canonical_runtime_journal()
    writer = EventJournalWriter()
    original_ref_ctx = _with_ref_ctx(repo, True)
    try:
        # Event 1 lands before cutover (mirror no-ops: not sqlite yet).
        written = writer._append_resolved(ResolvedEvent(
            root_id=root_id, sid=sid, event_type="agent_message",
            data=_tool_result_data(uid), source="test",
            ownership=MessageOwnership(msg_id), event_id=uid,
            run_id="run-A", turn_id="turn-A",
        ))
        assert written.seq >= 0, f"ingest failed: seq={written.seq}"
        # Cut the root over on the rows so far (as the first BFF read
        # does in production) — journal_through_seq now covers seq 1.
        rows_so_far, _, _ = ing.read_events(root_id, after_seq=0, limit=100)
        generation = journal.ensure_cutover(
            root_id, rows=rows_so_far, session={"id": root_id},
        )
        assert journal.is_authoritative(root_id), "cutover did not commit"
        # Event 2 (no top-level uuid in data) is LIVE-MIRRORED under
        # sqlite authority — this exercises the mirror row construction
        # whose skew from the disk row is the bug under test.
        written = writer._append_resolved(ResolvedEvent(
            root_id=root_id, sid=sid, event_type="agent_message",
            data=_tool_result_data_no_uuid(), source="test",
            ownership=MessageOwnership(msg_id), event_id=str(uuid.uuid4()),
            run_id="run-A", turn_id="turn-A",
        ))
        assert written.seq >= 0, f"ingest failed: seq={written.seq}"
        # Snapshot coverage + facts while the singleton journal is
        # still open (writer.close() closes its catalog connection).
        disk_rows, _, _ = ing.read_events(root_id, after_seq=0, limit=100)
        assert len(disk_rows) == 2, f"expected 2 disk rows, got {len(disk_rows)}"
        covered = journal.current_authority(root_id).journal_through_seq
        assert covered == disk_rows[-1]["seq"], (
            f"live mirror did not advance coverage: {covered} != {disk_rows[-1]['seq']}"
        )
        facts_before = journal._store().read(
            root_id, generation, after_seq=0, limit=1000,
        )
    finally:
        _restore_ref_ctx(original_ref_ctx)
        writer.close()
    ing.close_all()

    def _fact_map(facts):
        return {
            (
                f.fact.source_stream_id,
                f.fact.source_event_id,
                f.fact.source_order.generation,
                f.fact.source_order.sequence,
            ): (f.fact.payload_type, f.fact.content_hash, f.fact.turn_id)
            for f in facts
        }

    assert facts_before, "mirror_event wrote no facts under sqlite authority"
    mirror_streams = {
        f.fact.source_stream_id for f in facts_before
        if f.fact.source_order.sequence == disk_rows[-1]["seq"]
    }
    assert mirror_streams == {"run-A"}, (
        f"live-mirrored fact stream identity skewed: {mirror_streams}"
    )

    # Authority reset: fresh catalog, same surviving fact store.
    reset_journal = CanonicalRuntimeJournal(
        catalog_path=Path(_TMP_HOME) / "reset-authority-catalog.sqlite",
    )
    try:
        reset_generation = reset_journal.ensure_cutover(
            root_id, rows=disk_rows, session={"id": root_id},
        )
    except Exception as exc:
        print(f"  post-reset re-derivation raised: {type(exc).__name__}: {exc}")
        return False
    facts_after = reset_journal._store().read(
        root_id, reset_generation, after_seq=0, limit=1000,
    )

    before, after = _fact_map(facts_before), _fact_map(facts_after)
    ok = (
        reset_generation == generation
        and len(facts_after) == len(facts_before)
        and before == after
    )
    if not ok:
        print(f"  generations: {generation} -> {reset_generation}")
        print(f"  fact count: {len(facts_before)} -> {len(facts_after)}")
        for key in sorted(set(before) | set(after), key=str):
            if before.get(key) != after.get(key):
                print(f"  DIVERGED {key}:\n    before={before.get(key)}\n    after ={after.get(key)}")
    return ok


TESTS = [
    (
        "_append_resolved mirrors rewritten data into mirror_event, not raw event.data",
        test_append_resolved_mirrors_rewritten_data,
    ),
    (
        "mirrored and disk-derived content_hash agree post-fix "
        "(and would have disagreed pre-fix)",
        test_mirrored_and_disk_content_hash_agree_and_would_not_have_pre_fix,
    ),
    (
        "catalog reset + full re-derivation is identity-stable "
        "(run_id stream, source_event_id, turn_id parity)",
        test_catalog_reset_rederivation_is_identity_stable,
    ),
]


def main() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                ok = fn()
            except Exception as exc:
                ok = False
                print(f"  exception: {exc}")
            print(f"{PASS if ok else FAIL}  {name}")
            if not ok:
                failed += 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
        ing.close_all()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
