import os
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="ba-runtime-journal-"))
os.environ["BETTER_AGENT_HOME"] = str(HOME)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from canonical_runtime_journal import CanonicalRuntimeJournal


def test_import_cutover_mirror_and_page_read():
    root_id = "root"
    session = {
        "id": root_id,
        "messages": [
            {"id": "u1", "seq": 1, "role": "user", "content": "work"},
            {"id": "a1", "seq": 2, "role": "assistant", "content": "done"},
        ],
    }
    journal = CanonicalRuntimeJournal(HOME / "catalog.sqlite")
    rows = [{
        "root_id": root_id, "sid": root_id, "seq": 1, "type": "agent_message",
        "source": "claude", "msg_id": "a1",
        "data": {"uuid": "e1", "type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
    }]
    generation = journal.ensure_cutover(
        root_id, rows=rows, session=session,
    )
    first = journal.read_page(root_id, after_seq=0, limit=100)
    assert first["root_generation"] == generation
    assert {fact["payload_type"] for fact in first["facts"]} == {
        "user_prompt", "message_ownership_declared", "assistant_output",
    }
    journal.mirror_event(
        root_id=root_id, sid=root_id, seq=2, event_type="turn_complete",
        data={"message_id": "a1"}, source="claude", msg_id="a1",
        event_id="e2", turn_id="u1",
    )
    second = journal.read_page(
        root_id, after_seq=first["canonical_through_seq"], limit=100,
    )
    assert [fact["payload_type"] for fact in second["facts"]] == ["turn_complete"]
    journal.close()


def test_reconcile_gap_and_new_messages_after_cutover():
    root_id = "reconcile-root"
    session = {
        "id": root_id,
        "messages": [
            {"id": "u1", "seq": 1, "role": "user", "content": "first"},
            {"id": "a1", "seq": 2, "role": "assistant", "content": "answer"},
        ],
    }
    first_row = {
        "root_id": root_id, "sid": root_id, "seq": 1,
        "type": "agent_message", "source": "claude", "msg_id": "a1",
        "data": {"uuid": "e1", "type": "assistant", "message": {
            "content": [{"type": "text", "text": "answer"}],
        }},
    }
    journal = CanonicalRuntimeJournal(HOME / "reconcile-catalog.sqlite")
    generation = journal.ensure_cutover(root_id, rows=[first_row], session=session)

    session["messages"].extend([
        {"id": "u2", "seq": 3, "role": "user", "content": "second"},
        {"id": "a2", "seq": 4, "role": "assistant", "content": "later"},
    ])
    missed_mirror = {
        "root_id": root_id, "sid": root_id, "seq": 2,
        "type": "turn_complete", "source": "claude", "msg_id": "a1",
        "data": {"uuid": "e2", "message_id": "a1"},
    }
    assert journal.ensure_cutover(
        root_id, rows=[first_row, missed_mirror], session=session,
    ) == generation
    page = journal.read_page(root_id, after_seq=0, limit=100)
    payloads = [fact["payload_type"] for fact in page["facts"]]
    assert payloads.count("user_prompt") == 2
    assert payloads.count("message_ownership_declared") == 2
    assert "turn_complete" in payloads

    journal.close()


def test_fork_messages_emit_scoped_facts_with_per_node_heads():
    root_id = "forked-root"
    session = {
        "id": root_id,
        "messages": [
            {"id": "u1", "seq": 1, "role": "user", "content": "root work"},
            {"id": "a1", "seq": 2, "role": "assistant", "content": "root done"},
        ],
        "forks": [{
            "id": "fork-1",
            "messages": [
                # Copied pre-fork prefix (same ids/seqs as the root) plus the
                # fork's own tail; tail seqs collide with root seq numbers.
                {"id": "u1", "seq": 1, "role": "user", "content": "root work"},
                {"id": "a1", "seq": 2, "role": "assistant", "content": "root done"},
                {"id": "fu1", "seq": 3, "role": "user", "content": "fork work"},
                {"id": "fa1", "seq": 4, "role": "assistant", "content": "fork done"},
            ],
            "forks": [],
        }],
    }
    journal = CanonicalRuntimeJournal(HOME / "forked-catalog.sqlite")
    journal.ensure_cutover(root_id, rows=[], session=session)
    page = journal.read_page(root_id, after_seq=0, limit=100)
    prompts = {
        fact["payload"]["message_id"]: fact["sid"]
        for fact in page["facts"] if fact["payload_type"] == "user_prompt"
    }
    # The copied prefix dedups onto the root's fact (root-first walk); the
    # fork's own tail is scoped to the fork node.
    assert prompts == {"u1": root_id, "fu1": "fork-1"}
    heads = journal.current_authority(root_id).message_heads
    assert heads == {root_id: 2, "fork-1": 4}

    # A fork-only advance re-triggers emission for the fork node alone.
    session["forks"][0]["messages"].extend([
        {"id": "fu2", "seq": 5, "role": "user", "content": "fork again"},
        {"id": "fa2", "seq": 6, "role": "assistant", "content": "fork again done"},
    ])
    journal.ensure_cutover(root_id, rows=[], session=session)
    page = journal.read_page(root_id, after_seq=0, limit=100)
    fork_prompts = [
        fact["payload"]["message_id"]
        for fact in page["facts"]
        if fact["payload_type"] == "user_prompt" and fact["sid"] == "fork-1"
    ]
    assert fork_prompts == ["fu1", "fu2"]
    assert journal.current_authority(root_id).message_heads == {root_id: 2, "fork-1": 6}
    journal.close()


def test_delete_tombstones_generation_and_reuse_mints_next_generation():
    root_id = "reused-root"
    session = {"id": root_id, "messages": []}
    journal = CanonicalRuntimeJournal(HOME / "delete-catalog.sqlite")
    first_generation = journal.ensure_cutover(root_id, rows=[], session=session)
    deletion_generation = journal.begin_delete_root(root_id)
    journal.finish_delete_root(root_id, deletion_generation)
    assert not journal.is_authoritative(root_id)
    second_generation = journal.ensure_cutover(root_id, rows=[], session=session)
    assert second_generation == first_generation + 1
    journal.close()


def test_gap_skips_write_and_waits_for_read_path_resync():
    """mirror_event never repairs a gap itself -- it's a hot write path,
    and the canonical journal is a rebuildable, on-demand projection of
    jsonl. On a gap it must skip the write (no raise, no partial
    coverage) and leave repair to the read path (`ensure_cutover`,
    invoked by `ensure_canonical_authority_sync` before every BFF
    read)."""
    root_id = "gap-root"
    session = {"id": root_id, "messages": []}
    journal = CanonicalRuntimeJournal(HOME / "gap-catalog.sqlite")
    row1 = {"root_id": root_id, "sid": root_id, "seq": 1, "type": "turn_start", "data": {"uuid": "g1"}}
    journal.ensure_cutover(root_id, rows=[row1], session=session)

    # seq 2 is missing on disk too (not just unmirrored) -- mirror_event
    # must not raise, it must simply decline to advance coverage.
    journal.mirror_event(
        root_id=root_id, sid=root_id, seq=3, event_type="turn_complete",
        data={}, source="claude", msg_id=None, event_id="g3", turn_id=None,
    )
    assert journal.current_authority(root_id).journal_through_seq == 1
    page = journal.read_page(root_id, after_seq=0, limit=100)
    assert {fact["payload"]["uuid"] for fact in page["facts"]} == {"g1"}

    # The read path (ensure_cutover, as called by
    # ensure_canonical_authority_sync before every projection-source
    # read) fills the gap on demand once jsonl actually has the rows.
    row2 = {"root_id": root_id, "sid": root_id, "seq": 2, "type": "progress", "data": {"uuid": "g2"}}
    row3 = {"root_id": root_id, "sid": root_id, "seq": 3, "type": "turn_complete", "data": {"uuid": "g3"}}
    journal.ensure_cutover(root_id, rows=[row1, row2, row3], session=session)
    page = journal.read_page(root_id, after_seq=0, limit=100)
    assert {fact["payload"]["uuid"] for fact in page["facts"]} == {"g1", "g2", "g3"}
    journal.close()


def test_mirror_gap_leaves_coverage_for_read_path_to_fill():
    """A mirror_event call can be dropped for reasons unrelated to seq
    contiguity (transient store error, a fire-and-forget caller that
    never awaited/checked the result) even though the jsonl rows were
    already durably written. The next live mirror_event call for a
    later seq must not raise and must not attempt to repair inline --
    it just leaves journal_through_seq where it is. Coverage is only
    guaranteed again once something runs the read-path resync
    (ensure_cutover) against the current jsonl contents."""
    from event_ingester import event_ingester as ingester

    root_id = "gap-heal-root"
    session = {"id": root_id, "messages": []}

    seq1 = ingester.ingest(
        root_id, sid=root_id, event_type="turn_start", data={"uuid": "h1"},
        source="test",
    )
    assert seq1 == 1

    journal = CanonicalRuntimeJournal(HOME / "gap-heal-catalog.sqlite")
    rows, _, _ = ingester.read_events(root_id, after_seq=0, limit=100)
    journal.ensure_cutover(root_id, rows=rows, session=session)

    # Two events land on disk but their mirror is never invoked --
    # simulates a dropped/failed `mirror_event` call for those seqs.
    seq2 = ingester.ingest(
        root_id, sid=root_id, event_type="progress", data={"uuid": "h2"},
        source="test",
    )
    seq3 = ingester.ingest(
        root_id, sid=root_id, event_type="progress", data={"uuid": "h3"},
        source="test",
    )
    assert (seq2, seq3) == (2, 3)
    assert journal.current_authority(root_id).journal_through_seq == 1

    seq4 = ingester.ingest(
        root_id, sid=root_id, event_type="turn_complete", data={"uuid": "h4"},
        source="test",
    )
    assert seq4 == 4

    # The live mirror call for seq 4 arrives with journal_through_seq
    # still stuck at 1 -- it must not raise, and must not advance
    # coverage on its own.
    journal.mirror_event(
        root_id=root_id, sid=root_id, seq=seq4, event_type="turn_complete",
        data={"uuid": "h4"}, source="test", msg_id=None, event_id="h4",
        turn_id=None,
    )
    assert journal.current_authority(root_id).journal_through_seq == 1

    # Only the read-path resync (what ensure_canonical_authority_sync
    # runs before serving projection-source) catches the projection up
    # to what's actually on disk.
    rows, _, _ = ingester.read_events(root_id, after_seq=0, limit=100)
    journal.ensure_cutover(root_id, rows=rows, session=session)

    authority = journal.current_authority(root_id)
    assert authority.journal_through_seq == 4
    page = journal.read_page(root_id, after_seq=0, limit=100)
    assert {fact["payload"]["uuid"] for fact in page["facts"]} == {
        "h1", "h2", "h3", "h4",
    }
    journal.close()


def test_sequence_zero_is_not_skipped():
    root_id = "zero-root"
    row = {"root_id": root_id, "sid": root_id, "seq": 0, "type": "turn_start", "data": {"uuid": "zero"}}
    journal = CanonicalRuntimeJournal(HOME / "zero-catalog.sqlite")
    journal.ensure_cutover(root_id, rows=[row], session={"id": root_id, "messages": []})
    page = journal.read_page(root_id, after_seq=0, limit=100)
    assert [fact["payload"]["uuid"] for fact in page["facts"]] == ["zero"]
    journal.close()


def test_unchanged_steady_read_does_no_store_work_or_fsync():
    root_id = "steady-root"
    session = {"id": root_id, "messages": [{"id": "u1", "seq": 1, "role": "user", "content": "x"}]}
    journal = CanonicalRuntimeJournal(HOME / "steady-catalog.sqlite")
    journal.ensure_cutover(root_id, rows=[], session=session)
    journal._store = lambda: (_ for _ in ()).throw(AssertionError("store touched"))
    journal._fsync_database = lambda _: (_ for _ in ()).throw(AssertionError("fsync touched"))
    journal.ensure_cutover(root_id, rows=[], session=session)
    journal.close()


def test_interrupted_delete_resolves_from_durable_session_presence():
    import session_store

    root_id = "pending-delete-root"
    journal = CanonicalRuntimeJournal(HOME / "pending-delete-catalog.sqlite")
    generation = journal.ensure_cutover(root_id, rows=[], session={"id": root_id, "messages": []})
    path = Path(session_store.session_file_path(root_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    journal.begin_delete_root(root_id)
    journal.resolve_pending_deletions()
    assert journal.is_authoritative(root_id)

    journal.begin_delete_root(root_id)
    path.unlink()
    journal.resolve_pending_deletions()
    assert not journal.is_authoritative(root_id)
    assert journal.ensure_cutover(
        root_id, rows=[], session={"id": root_id, "messages": []},
    ) == generation + 1
    journal.close()


def test_reconciliation_rejects_non_contiguous_duplicate_and_malformed_sequences():
    root_id = "invalid-tail-root"
    session = {"id": root_id, "messages": []}
    invalid_tails = [
        [{"seq": 1, "sid": root_id, "type": "x", "data": {}},
         {"seq": 3, "sid": root_id, "type": "x", "data": {}}],
        [{"seq": 1, "sid": root_id, "type": "x", "data": {}},
         {"seq": 1, "sid": root_id, "type": "x", "data": {}}],
        [{"seq": "1", "sid": root_id, "type": "x", "data": {}}],
    ]
    for index, rows in enumerate(invalid_tails):
        journal = CanonicalRuntimeJournal(HOME / f"invalid-tail-{index}.sqlite")
        try:
            journal.ensure_cutover(root_id, rows=rows, session=session)
            raise AssertionError("invalid reconciliation tail must fail")
        except Exception as exc:
            assert "reconciliation" in str(exc)
        journal.close()


def test_zero_fact_live_row_advances_contiguous_coverage():
    root_id = "zero-fact-root"
    journal = CanonicalRuntimeJournal(HOME / "zero-fact-catalog.sqlite")
    journal.ensure_cutover(
        root_id,
        rows=[{"root_id": root_id, "sid": root_id, "seq": 1, "type": "turn_start", "data": {"uuid": "z1"}}],
        session={"id": root_id, "messages": []},
    )
    journal.mirror_event(
        root_id=root_id, sid=root_id, seq=2, event_type="agent_message",
        data={"uuid": "z2", "type": "assistant", "message": {"content": []}},
        source="claude", msg_id="a1", event_id="z2", turn_id="u1",
    )
    assert journal.current_authority(root_id).journal_through_seq == 2
    journal.mirror_event(
        root_id=root_id, sid=root_id, seq=3, event_type="turn_complete",
        data={}, source="claude", msg_id="a1", event_id="z3", turn_id="u1",
    )
    assert journal.current_authority(root_id).journal_through_seq == 3
    journal.close()


def test_provider_stream_render_rows_do_not_enter_canonical_projection():
    root_id = "provider-stream-root"
    session = {
        "id": root_id,
        "messages": [
            {"id": "u1", "seq": 1, "role": "user", "content": "first"},
            {"id": "a1", "seq": 2, "role": "assistant", "content": "stream copy"},
            {"id": "u2", "seq": 3, "role": "user", "content": "second"},
            {"id": "a2", "seq": 4, "role": "assistant", "content": "authoritative"},
        ],
    }
    duplicate_stream = {
        "root_id": root_id, "sid": root_id, "seq": 1,
        "type": "agent_message", "source": "provider_stream", "msg_id": "a1",
        "data": {"uuid": "dup", "type": "assistant", "message": {
            "content": [{"type": "text", "text": "same"}],
        }},
    }
    authoritative_apply = {
        "root_id": root_id, "sid": root_id, "seq": 2,
        "type": "agent_message", "source": "apply_event", "msg_id": "a2",
        "data": {"uuid": "dup", "type": "assistant", "message": {
            "content": [{"type": "text", "text": "same"}],
        }},
    }
    provider_stream_non_render = {
        "root_id": root_id, "sid": root_id, "seq": 3,
        "type": "turn_complete", "source": "provider_stream", "msg_id": "a2",
        "data": {"uuid": "complete", "message_id": "a2"},
    }
    ownership = {
        "root_id": root_id, "sid": root_id, "seq": 4,
        "type": "event_ownership_resolved", "source": "provider_stream", "msg_id": "a2",
        "data": {"uuid": "ownership", "message_id": "a2"},
    }
    journal = CanonicalRuntimeJournal(HOME / "provider-stream-catalog.sqlite")
    journal.ensure_cutover(
        root_id,
        rows=[
            duplicate_stream,
            authoritative_apply,
            provider_stream_non_render,
            ownership,
        ],
        session=session,
    )
    page = journal.read_page(root_id, after_seq=0, limit=100)
    facts = page["facts"]
    duplicate_facts = [
        fact for fact in facts
        if fact["source_event_id"] == "dup"
    ]
    assert len(duplicate_facts) == 1
    assert duplicate_facts[0]["source"] == "apply_event"
    assert duplicate_facts[0]["payload"]["message_id"] == "a2"
    assert not any(
        fact["source"] == "provider_stream"
        and fact["payload_type"] == "agent_message"
        for fact in facts
    )
    assert any(
        fact["source_event_id"] == "complete"
        and fact["source"] == "provider_stream"
        for fact in facts
    )
    assert any(
        fact["payload_type"] == "event_ownership_resolved"
        and fact["source"] == "provider_stream"
        for fact in facts
    )
    assert journal.current_authority(root_id).journal_through_seq == 4
    journal.close()


def test_live_provider_stream_render_row_advances_without_fact():
    root_id = "live-provider-stream-root"
    journal = CanonicalRuntimeJournal(HOME / "live-provider-stream-catalog.sqlite")
    journal.ensure_cutover(root_id, rows=[], session={"id": root_id, "messages": []})
    journal.mirror_event(
        root_id=root_id, sid=root_id, seq=0, event_type="agent_message",
        data={"uuid": "stream-only", "type": "assistant", "message": {
            "content": [{"type": "text", "text": "skip"}],
        }},
        source="provider_stream", msg_id="a1", event_id="stream-only", turn_id="u1",
    )
    assert journal.current_authority(root_id).journal_through_seq == 0
    assert journal.read_page(root_id, after_seq=0, limit=100)["facts"] == []
    journal.mirror_event(
        root_id=root_id, sid=root_id, seq=1, event_type="agent_message",
        data={"uuid": "apply", "type": "assistant", "message": {
            "content": [{"type": "text", "text": "keep"}],
        }},
        source="apply_event", msg_id="a1", event_id="apply", turn_id="u1",
    )
    page = journal.read_page(root_id, after_seq=0, limit=100)
    assert [fact["source_event_id"] for fact in page["facts"]] == ["apply"]
    journal.close()


def test_event_writer_reads_jsonl_only_after_catalog_cursor():
    from unittest import mock
    from event_journal import EventJournalWriter

    root_id = "production-tail-root"
    session = {"id": root_id, "messages": []}
    row = {"root_id": root_id, "sid": root_id, "seq": 1, "type": "turn_start", "data": {"uuid": "p1"}}
    cursors: list[int] = []

    def read_events(_root_id, *, after_seq, limit):
        cursors.append(after_seq)
        return ([row], 1, False) if after_seq < 1 else ([], after_seq, False)

    writer = EventJournalWriter()
    with mock.patch("session_store.get_session", return_value=session), mock.patch(
        "event_journal.event_ingester.read_events", side_effect=read_events,
    ):
        writer.ensure_canonical_authority_sync(root_id)
        writer.ensure_canonical_authority_sync(root_id)
    assert cursors == [-1, 1]
    writer.close()


def test_first_singleton_access_does_not_wait_on_root_lifecycle_gate():
    import threading
    import canonical_runtime_journal as runtime_module
    from root_lifecycle import root_lifecycle_gate

    runtime_module.close_canonical_runtime_journal()
    initialized = threading.Event()
    with root_lifecycle_gate("blocked-root"):
        worker = threading.Thread(
            target=lambda: (
                runtime_module.canonical_runtime_journal(),
                initialized.set(),
            ),
        )
        worker.start()
        assert initialized.wait(2), "journal initialization deadlocked on root lifecycle gate"
    worker.join(2)
    assert not worker.is_alive()
    runtime_module.close_canonical_runtime_journal()


if __name__ == "__main__":
    test_import_cutover_mirror_and_page_read()
    test_reconcile_gap_and_new_messages_after_cutover()
    test_fork_messages_emit_scoped_facts_with_per_node_heads()
    test_delete_tombstones_generation_and_reuse_mints_next_generation()
    test_gap_skips_write_and_waits_for_read_path_resync()
    test_mirror_gap_leaves_coverage_for_read_path_to_fill()
    test_sequence_zero_is_not_skipped()
    test_unchanged_steady_read_does_no_store_work_or_fsync()
    test_interrupted_delete_resolves_from_durable_session_presence()
    test_reconciliation_rejects_non_contiguous_duplicate_and_malformed_sequences()
    test_zero_fact_live_row_advances_contiguous_coverage()
    test_provider_stream_render_rows_do_not_enter_canonical_projection()
    test_live_provider_stream_render_row_advances_without_fact()
    test_event_writer_reads_jsonl_only_after_catalog_cursor()
    test_first_singleton_access_does_not_wait_on_root_lifecycle_gate()
    print("canonical runtime journal tests passed")
