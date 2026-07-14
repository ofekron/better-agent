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


def test_gap_cannot_skip_missing_jsonl_row():
    root_id = "gap-root"
    session = {"id": root_id, "messages": []}
    journal = CanonicalRuntimeJournal(HOME / "gap-catalog.sqlite")
    row1 = {"root_id": root_id, "sid": root_id, "seq": 1, "type": "turn_start", "data": {"uuid": "g1"}}
    journal.ensure_cutover(root_id, rows=[row1], session=session)
    try:
        journal.mirror_event(
            root_id=root_id, sid=root_id, seq=3, event_type="turn_complete",
            data={}, source="claude", msg_id=None, event_id="g3", turn_id=None,
        )
        raise AssertionError("non-contiguous coverage must fail")
    except Exception as exc:
        assert "coverage gap" in str(exc)
    row2 = {"root_id": root_id, "sid": root_id, "seq": 2, "type": "progress", "data": {"uuid": "g2"}}
    row3 = {"root_id": root_id, "sid": root_id, "seq": 3, "type": "turn_complete", "data": {"uuid": "g3"}}
    journal.ensure_cutover(root_id, rows=[row1, row2, row3], session=session)
    page = journal.read_page(root_id, after_seq=0, limit=100)
    assert {fact["payload"]["uuid"] for fact in page["facts"]} == {"g1", "g2", "g3"}
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
    test_delete_tombstones_generation_and_reuse_mints_next_generation()
    test_gap_cannot_skip_missing_jsonl_row()
    test_sequence_zero_is_not_skipped()
    test_unchanged_steady_read_does_no_store_work_or_fsync()
    test_interrupted_delete_resolves_from_durable_session_presence()
    test_reconciliation_rejects_non_contiguous_duplicate_and_malformed_sequences()
    test_zero_fact_live_row_advances_contiguous_coverage()
    test_event_writer_reads_jsonl_only_after_catalog_cursor()
    test_first_singleton_access_does_not_wait_on_root_lifecycle_gate()
    print("canonical runtime journal tests passed")
