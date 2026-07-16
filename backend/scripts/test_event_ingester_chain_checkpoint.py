#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import shutil
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path

os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(prefix="ba-event-chain-")

import _test_home

_HOME = _test_home.engage(Path(os.environ["BETTER_AGENT_HOME"]), lock=False)

from event_ingester import EventIngester, _CHAIN_INTERVAL, _CHAIN_ZERO


def _append(ingester: EventIngester, root: str, uid: str, text: str = "x") -> int:
    return ingester.ingest(
        root, root, "agent_message",
        {"uuid": uid, "message": {"content": [{"type": "text", "text": text}]}},
        source="test", msg_id="msg", cwd_override="",
    )


def _manual_digest(path: Path, count: int) -> str:
    digest = _CHAIN_ZERO
    with path.open("rb") as handle:
        for _ in range(count):
            digest = hashlib.sha256(digest + handle.readline()).digest()
    return digest.hex()


def _commit(ingester: EventIngester, root: str, covered: int) -> tuple[dict, Path]:
    token = ingester.ownership_checkpoint_token(root)
    assert token is not None
    checkpoint = ingester._root_dir(root) / "ownership.json"
    fence = ingester.commit_ownership_snapshot(
        root,
        token=token,
        covered_seq=covered,
        checkpoint_path=checkpoint,
        payload={"version": 1, "root_id": root, "state": {"immutable": True}},
    )
    assert fence is not None
    return fence, checkpoint


def _migrate_in_process(
    root: str, path_raw: str, cwd: str,
    replace_entered, release_replace, invalidate_entered, release_invalidate,
) -> None:
    import file_ref_resolver
    import hydration_index_store

    original_atomic_write = file_ref_resolver._atomic_write_tmp
    original_invalidate = hydration_index_store.invalidate

    def gated_atomic_write(target: Path, text: str) -> None:
        replace_entered.set()
        assert release_replace.wait(5)
        original_atomic_write(target, text)

    def gated_invalidate(root_id: str, journal: Path) -> None:
        invalidate_entered.set()
        assert release_invalidate.wait(5)
        original_invalidate(root_id, journal)

    file_ref_resolver._atomic_write_tmp = gated_atomic_write
    hydration_index_store.invalidate = gated_invalidate
    assert file_ref_resolver._migrate_events_jsonl(
        root, Path(path_raw), cwd,
    )


def test_append_prefix_and_tail_only_restore() -> None:
    root = "append-prefix"
    ingester = EventIngester()
    assert _append(ingester, root, "u1", "A" * 9000) == 1
    assert _append(ingester, root, "u2") == 2
    assert _append(ingester, root, "u3") == 3
    fence, _ = _commit(ingester, root, 2)
    path = ingester._events_path(root)
    assert fence["digest"] == _manual_digest(path, 2)
    assert fence["covered_size"] == ingester._seq_offsets[root][2]
    cold = EventIngester()
    cold._ensure_open = lambda _root: (_ for _ in ()).throw(AssertionError("cold validation scanned journal"))
    assert cold.validate_ownership_checkpoint(root, fence)
    ingester.close(root)


def test_early_same_inode_mutation_invalidates_checkpoint() -> None:
    root = "early-mutation"
    ingester = EventIngester()
    _append(ingester, root, "u1", "A" * 10_000)
    _append(ingester, root, "u2")
    fence, _ = _commit(ingester, root, 2)
    path = ingester._events_path(root)
    before = path.stat()
    payload = bytearray(path.read_bytes())
    index = payload.index(b"AAAA") + 1
    payload[index] = ord("B")
    with path.open("r+b") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    after = path.stat()
    assert (before.st_dev, before.st_ino, before.st_size) == (after.st_dev, after.st_ino, after.st_size)
    cold = EventIngester()
    assert not cold.validate_ownership_checkpoint(root, fence)
    assert cold.ownership_checkpoint_token(root) is not None
    ingester.close(root)
    cold.close(root)


def test_cas_conflict_and_unresolved_prefix() -> None:
    root = "cas-conflict"
    ingester = EventIngester()
    _append(ingester, root, "u1")
    token = ingester.ownership_checkpoint_token(root)
    assert token is not None
    _append(ingester, root, "u2")
    checkpoint = ingester._root_dir(root) / "ownership.json"
    assert ingester.commit_ownership_snapshot(
        root, token=token, covered_seq=1, checkpoint_path=checkpoint,
        payload={"version": 1, "state": {}},
    ) is None
    fence, _ = _commit(ingester, root, 1)
    assert fence["covered_seq"] == 1 and fence["head_seq"] == 2
    ingester.close(root)


def test_truncate_torn_tail_replace_and_crash_gap() -> None:
    root = "repair"
    ingester = EventIngester()
    for index in range(4):
        _append(ingester, root, f"u{index}")
    fence, _ = _commit(ingester, root, 4)
    path = ingester._events_path(root)
    generation = fence["generation"]
    ingester.close(root)

    with path.open("ab") as handle:
        handle.write(b'{"seq":5,"torn"')
        handle.flush()
        os.fsync(handle.fileno())
    repaired = EventIngester()
    assert not repaired.validate_ownership_checkpoint(root, fence)
    assert path.read_bytes().endswith(b"\n")
    repaired_token = repaired.ownership_checkpoint_token(root)
    assert repaired_token is not None and repaired_token["generation"] > generation
    repaired.close(root)

    lines = path.read_bytes().splitlines(keepends=True)
    replacement = path.with_suffix(".compact")
    replacement.write_bytes(b"".join(
        json.dumps(json.loads(line), separators=(",", ":")).encode() + b"\n"
        for line in lines
    ))
    os.replace(replacement, path)
    compacted = EventIngester()
    compacted_token = compacted.ownership_checkpoint_token(root)
    assert compacted_token is not None and compacted_token["seq"] == 4
    compacted.close(root)

    with path.open("ab") as handle:
        handle.write(lines[0])
        handle.flush()
        os.fsync(handle.fileno())
    crashed = EventIngester()
    assert crashed.ownership_checkpoint_token(root)["seq"] == 5
    crashed.close(root)


def test_cached_handle_does_not_append_to_replaced_inode() -> None:
    root = "replace-live"
    ingester = EventIngester()
    _append(ingester, root, "u1")
    path = ingester._events_path(root)
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(path.read_bytes())
    os.replace(replacement, path)
    assert _append(ingester, root, "u2") == 2
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["seq"] for row in rows] == [1, 2]
    ingester.close(root)


def test_bcfile_migration_serializes_cross_process_live_append() -> None:
    import file_ref_resolver

    root = "migration-live-append"
    ingester = EventIngester()
    referenced = ingester._root_dir(root) / "referenced.py"
    referenced.parent.mkdir(parents=True, exist_ok=True)
    path = ingester._events_path(root)
    path.write_text(json.dumps({
        "seq": 1, "ts": "2026-01-01T00:00:00+00:00", "sid": root,
        "type": "agent_message", "source": "test", "msg_id": "msg",
        "data": {
            "uuid": "u1",
            "message": {"content": [{"type": "text", "text": str(referenced)}]},
        },
    }) + "\n", encoding="utf-8")
    referenced.touch()
    file_ref_resolver._cache.invalidate_path(str(referenced))
    context = multiprocessing.get_context("fork")
    replace_entered = context.Event()
    release_replace = context.Event()
    invalidate_entered = context.Event()
    release_invalidate = context.Event()
    append_started = threading.Event()
    append_done = threading.Event()

    def append() -> None:
        append_started.set()
        assert _append(ingester, root, "u2", "after migration") == 2
        append_done.set()

    migration_process = context.Process(
        target=_migrate_in_process,
        args=(
            root, str(path), str(path.parent), replace_entered, release_replace,
            invalidate_entered, release_invalidate,
        ),
    )
    append_thread = threading.Thread(target=append)
    try:
        migration_process.start()
        assert replace_entered.wait(5)
        append_thread.start()
        assert append_started.wait(5)
        time.sleep(0.02)
        assert not append_done.is_set()
        release_replace.set()
        assert invalidate_entered.wait(5)
        assert not append_done.is_set()
        release_invalidate.set()
        migration_process.join(5)
        append_thread.join(5)
        assert migration_process.exitcode == 0
        assert not append_thread.is_alive()
    finally:
        release_replace.set()
        release_invalidate.set()
        migration_process.join(5)
        if migration_process.is_alive():
            migration_process.terminate()
            migration_process.join(5)
        if append_thread.ident is not None:
            append_thread.join(5)

    assert _append(ingester, root, "u3", "identity refreshed") == 3
    ingester.close_all()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["seq"] for row in rows] == [1, 2, 3]
    assert "bcfile:" in rows[0]["data"]["message"]["content"][0]["text"]
    meta = json.loads(ingester._event_chain_path(root).read_text())
    assert meta["seq"] == 3
    assert meta["size"] == path.stat().st_size
    assert meta["digest"] == _manual_digest(path, 3)


def test_bcfile_migration_durability_order_and_failure() -> None:
    import file_ref_resolver
    import hydration_index_store

    root = "migration-durability-order"
    ingester = EventIngester()
    referenced = ingester._root_dir(root) / "referenced.py"
    referenced.parent.mkdir(parents=True, exist_ok=True)
    _append(ingester, root, "u1", str(referenced))
    ingester.close_all()
    referenced.touch()
    file_ref_resolver._cache.invalidate_path(str(referenced))
    path = ingester._events_path(root)
    order: list[str] = []
    original_fsync = os.fsync
    original_replace = os.replace
    original_invalidate = hydration_index_store.invalidate

    def observed_fsync(fd: int) -> None:
        order.append("fsync")
        original_fsync(fd)

    def observed_replace(source, target) -> None:
        source_path = Path(source)
        assert source_path != path
        assert source_path.parent == path.parent
        assert source_path.name.startswith(f".{path.name}.bcfile.")
        assert source_path.stat().st_mode & 0o777 == 0o600
        order.append("replace")
        original_replace(source, target)

    def observed_invalidate(root_id: str, journal: Path) -> None:
        assert root_id == root and journal == path
        order.append("invalidate")
        original_invalidate(root_id, journal)

    os.fsync = observed_fsync
    os.replace = observed_replace
    hydration_index_store.invalidate = observed_invalidate
    try:
        assert file_ref_resolver._migrate_events_jsonl(root, path, str(path.parent))
    finally:
        os.fsync = original_fsync
        os.replace = original_replace
        hydration_index_store.invalidate = original_invalidate
    assert order == ["fsync", "replace", "fsync", "invalidate"], order

    direct = path.parent / "direct-fault.txt"
    direct.write_text("old", encoding="utf-8")
    prefix = f".{direct.name}.bcfile."
    fsync_calls = 0

    def fail_file_fsync(fd: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        raise OSError("injected file fsync failure")

    os.fsync = fail_file_fsync
    try:
        try:
            file_ref_resolver._atomic_write_tmp(direct, "new")
        except OSError as exc:
            assert "injected file fsync failure" in str(exc)
        else:
            raise AssertionError("file fsync failure was swallowed")
    finally:
        os.fsync = original_fsync
    assert direct.read_text(encoding="utf-8") == "old"
    assert not any(item.name.startswith(prefix) for item in direct.parent.iterdir())

    def fail_replace(_source, _target) -> None:
        raise OSError("injected replace failure")

    os.replace = fail_replace
    try:
        try:
            file_ref_resolver._atomic_write_tmp(direct, "new")
        except OSError as exc:
            assert "injected replace failure" in str(exc)
        else:
            raise AssertionError("replace failure was swallowed")
    finally:
        os.replace = original_replace
    assert direct.read_text(encoding="utf-8") == "old"
    assert not any(item.name.startswith(prefix) for item in direct.parent.iterdir())

    second = ingester._root_dir(root) / "second.py"
    _append(ingester, root, "u2", str(second))
    ingester.close_all()
    second.touch()
    file_ref_resolver._cache.invalidate_path(str(second))
    invalidated = False
    fsync_count = 0

    def fail_directory_fsync(fd: int) -> None:
        nonlocal fsync_count
        fsync_count += 1
        if fsync_count == 2:
            raise OSError("injected directory fsync failure")
        original_fsync(fd)

    def observed_failure_invalidate(root_id: str, journal: Path) -> None:
        nonlocal invalidated
        invalidated = True
        original_invalidate(root_id, journal)

    os.fsync = fail_directory_fsync
    hydration_index_store.invalidate = observed_failure_invalidate
    try:
        try:
            file_ref_resolver._migrate_events_jsonl(root, path, str(path.parent))
        except OSError as exc:
            assert "injected directory fsync failure" in str(exc)
        else:
            raise AssertionError("directory fsync failure was swallowed")
    finally:
        os.fsync = original_fsync
        hydration_index_store.invalidate = original_invalidate
    assert invalidated
    assert not hydration_index_store._db_path(root).exists()
    offsets, _ = hydration_index_store.load(root, path)
    assert len(offsets[root]) == 2 and offsets[root][0] == 0


def test_sparse_memory_bounded_tail_and_no_caller_fsync() -> None:
    root = "sparse-large"
    ingester = EventIngester()
    caller_fsyncs = 0
    original_fsync = os.fsync

    def counted_fsync(fd: int) -> None:
        nonlocal caller_fsyncs
        if threading.current_thread() is threading.main_thread():
            caller_fsyncs += 1
        original_fsync(fd)

    os.fsync = counted_fsync
    try:
        for index in range(5_000):
            _append(ingester, root, f"large-{index}")
    finally:
        os.fsync = original_fsync
    # Appenders never pay stable-storage latency; only the coalescing
    # background flusher may fsync the journal/chain metadata.
    assert caller_fsyncs <= 3, caller_fsyncs
    ladder = ingester._chain_digests[root]
    assert len(ladder) == 5_000 // _CHAIN_INTERVAL
    assert len(ladder) < 5_000 / 100
    covered = 4_873
    nearest = max((int(point["seq"]) for point in ladder if int(point["seq"]) <= covered), default=0)
    assert covered - nearest < _CHAIN_INTERVAL
    fence, _ = _commit(ingester, root, covered)
    assert fence["digest"] == _manual_digest(ingester._events_path(root), covered)
    ingester.close(root)
    cold = EventIngester()
    cold_fence, _ = _commit(cold, root, covered + 1)
    assert len(cold._chain_digests[root]) == 5_000 // _CHAIN_INTERVAL
    assert cold_fence["digest"] == _manual_digest(cold._events_path(root), covered + 1)
    cold.close(root)


def test_append_during_background_fsync_keeps_new_epoch_dirty() -> None:
    root = "append-during-fsync"
    ingester = EventIngester()
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()
    second_release = threading.Event()
    original_fsync = os.fsync
    journal_fsyncs = 0

    def gated_fsync(fd: int) -> None:
        nonlocal journal_fsyncs
        if threading.current_thread().name == "event-ingester-fsync":
            try:
                is_journal = os.fstat(fd).st_ino == ingester._events_path(root).stat().st_ino
            except OSError:
                is_journal = False
            if is_journal:
                journal_fsyncs += 1
                if journal_fsyncs == 1:
                    entered.set()
                    assert release.wait(5)
                elif journal_fsyncs == 2:
                    second_entered.set()
                    assert second_release.wait(5)
        original_fsync(fd)

    os.fsync = gated_fsync
    try:
        _append(ingester, root, "u1")
        assert entered.wait(5)
        append_done = threading.Event()

        def append_second() -> None:
            _append(ingester, root, "u2")
            append_done.set()

        thread = threading.Thread(target=append_second)
        thread.start()
        time.sleep(0.02)
        assert not append_done.is_set()
        release.set()
        thread.join(5)
        assert append_done.is_set()
        assert second_entered.wait(5)
        with ingester._fsync_cond:
            assert root in ingester._fsync_dirty
    finally:
        release.set()
        second_release.set()
        os.fsync = original_fsync
    ingester.close_all()
    meta = json.loads(ingester._event_chain_path(root).read_text())
    assert meta["seq"] == 2
    assert meta["size"] == ingester._events_path(root).stat().st_size


def test_cold_sparse_read_seeks_from_validated_ladder() -> None:
    root = "cold-sparse-read"
    ingester = EventIngester()
    for index in range(_CHAIN_INTERVAL * 4 + 17):
        _append(ingester, root, f"u{index}")
    ingester.close_all()

    class ObservedIngester(EventIngester):
        sparse_start: int | None = None

        def _scan_from(self, path, root_id, start_offset, after_seq, *args, **kwargs):
            self.sparse_start = start_offset
            return super()._scan_from(
                path, root_id, start_offset, after_seq, *args, **kwargs,
            )

    cold = ObservedIngester()
    after = _CHAIN_INTERVAL * 4 + 3
    rows, _, _ = cold.read_events(root, after_seq=after, limit=100)
    ladder = json.loads(cold._event_chain_path(root).read_text())["ladder"]
    expected = max(point["size"] for point in ladder if point["seq"] <= after)
    assert cold.sparse_start == expected
    assert [row["seq"] for row in rows] == list(range(after + 1, _CHAIN_INTERVAL * 4 + 18))
    assert after - max(point["seq"] for point in ladder if point["seq"] <= after) < _CHAIN_INTERVAL
    cold.close(root)


def test_corrupt_sparse_ladder_fails_closed_and_rebuilds() -> None:
    root = "corrupt-sparse-ladder"
    ingester = EventIngester()
    for index in range(_CHAIN_INTERVAL + 1):
        _append(ingester, root, f"u{index}")
    ingester.close_all()
    meta_path = ingester._event_chain_path(root)
    meta = json.loads(meta_path.read_text())
    meta["ladder"][0]["size"] = meta["size"] + 1
    meta["ladder_checksum"] = hashlib.sha256(
        json.dumps(meta["ladder"], separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    meta_path.write_text(json.dumps(meta, separators=(",", ":")))
    cold = EventIngester()
    rows, _, _ = cold.read_events(root, after_seq=_CHAIN_INTERVAL, limit=10)
    assert [row["seq"] for row in rows] == [_CHAIN_INTERVAL + 1]
    assert cold._seq[root] == _CHAIN_INTERVAL + 1
    cold.close(root)


def test_corrupt_cold_checkpoint_rebuild_has_bounded_projection_memory() -> None:
    root = "bounded-corrupt-rebuild"
    ingester = EventIngester()
    for index in range(5_000):
        _append(ingester, root, f"u{index}", "x" * 256)
    ingester.close_all()
    meta_path = ingester._event_chain_path(root)
    meta = json.loads(meta_path.read_text())
    meta["ladder_checksum"] = "0" * 64
    meta_path.write_text(json.dumps(meta, separators=(",", ":")))

    cold = EventIngester()
    tracemalloc.start()
    token = cold.ownership_checkpoint_token(root)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert token is not None and token["seq"] == 5_000
    assert root not in cold._seq_offsets
    assert root not in cold._seen_event_owners
    assert len(cold._chain_digests[root]) == 5_000 // _CHAIN_INTERVAL
    assert peak < 2_000_000, peak
    cold.close(root)


def test_event_journal_writer_hydrates_from_one_sparse_interval() -> None:
    import event_journal

    root = "ejw-sparse-hydrate"
    ingester = EventIngester()
    for index in range(5_000):
        _append(ingester, root, f"u{index}")
    original = event_journal.event_ingester
    event_journal.event_ingester = ingester
    writer = event_journal.EventJournalWriter()
    try:
        writer._write_ownership_checkpoint(root, 4_873)
        ingester.close_all()
        cold = EventIngester()
        event_journal.event_ingester = cold
        restored = event_journal.EventJournalWriter()
        starts: list[int] = []
        original_scan = cold._scan_from

        def observed_scan(path, root_id, start_offset, after_seq, *args, **kwargs):
            starts.append(start_offset)
            return original_scan(path, root_id, start_offset, after_seq, *args, **kwargs)

        cold._scan_from = observed_scan
        restored._hydrate_snapshot_turn_boundaries = lambda _root: False
        restored._ensure_ownership_hydrated(root)
        assert len(starts) == 1
        with cold._events_path(root).open("rb") as source:
            source.seek(starts[0])
            parsed_from_seek = sum(1 for _ in source)
        assert parsed_from_seek <= _CHAIN_INTERVAL
        assert root not in cold._seq_offsets
        restored.close()
        cold.close(root)
    finally:
        writer.close()
        event_journal.event_ingester = original


def main() -> None:
    try:
        test_append_prefix_and_tail_only_restore()
        test_early_same_inode_mutation_invalidates_checkpoint()
        test_cas_conflict_and_unresolved_prefix()
        test_truncate_torn_tail_replace_and_crash_gap()
        test_cached_handle_does_not_append_to_replaced_inode()
        test_sparse_memory_bounded_tail_and_no_caller_fsync()
        test_append_during_background_fsync_keeps_new_epoch_dirty()
        test_bcfile_migration_serializes_cross_process_live_append()
        test_bcfile_migration_durability_order_and_failure()
        test_cold_sparse_read_seeks_from_validated_ladder()
        test_corrupt_sparse_ladder_fails_closed_and_rebuilds()
        test_corrupt_cold_checkpoint_rebuild_has_bounded_projection_memory()
        test_event_journal_writer_hydrates_from_one_sparse_interval()
        print("PASS event ingester chained checkpoint durability")
    finally:
        shutil.rmtree(_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
