from __future__ import annotations

import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import threading
import tempfile
import os
import symtable
import sys
import time
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import atexit
import shutil

import paths

_TEST_HOME = tempfile.mkdtemp(prefix="ba-event-loop-regressions-")
paths.engage_test_home(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, True)

import runtime_ownership

runtime_ownership.register_current_process_writer()


class _ModeEchoStrategy:
    def __init__(self, mode: str):
        self.mode = mode

    def build_assistant_scaffold(self) -> dict:
        return {"mode": self.mode}


def test_session_projection_uses_bounded_off_loop_drainer() -> None:
    import event_bus_subscribers
    from event_bus import BusEvent
    from event_bus_subscribers import SessionProjectionDrainer

    applied = threading.Event()
    release = threading.Event()
    applying_threads: list[str] = []
    applied_seqs: list[int] = []
    dirty_roots: list[str] = []

    def apply_row(_root_id: str, _row: dict) -> None:
        applying_threads.append(threading.current_thread().name)
        applied_seqs.append(int(_row["seq"]))
        if _row["seq"] == 1:
            applied.set()
            assert release.wait(2)

    rows = [{
        "seq": seq,
        "sid": "root",
        "msg_id": "msg",
        "type": "agent_message",
        "source": "event_bus",
        "data": {},
    } for seq in (1, 2)]

    def read_rows(root_id: str, after_seq: int, limit: int) -> list[dict]:
        if root_id != "root":
            return []
        return [row for row in rows if row["seq"] > after_seq][:limit]

    drainer = SessionProjectionDrainer(
        apply_row,
        read_rows,
        lambda root_id, _exc: dirty_roots.append(root_id),
        max_active_roots=1,
        chunk_size=1,
    )
    original = event_bus_subscribers._SESSION_PROJECTION_DISPATCHER
    event_bus_subscribers._SESSION_PROJECTION_DISPATCHER = drainer
    try:
        async def run() -> None:
            ticks = 0

            async def ticker() -> None:
                nonlocal ticks
                while not release.is_set():
                    ticks += 1
                    await asyncio.sleep(0)

            ticker_task = asyncio.create_task(ticker())
            await event_bus_subscribers._refresh_session_content_projection(BusEvent(
                type="event_journal.written",
                root_id="root",
                sid="root",
                msg_id="msg",
                payload={"seq": 1, "event_type": "agent_message", "source": "event_bus"},
                persist=False,
            ))
            assert await asyncio.to_thread(applied.wait, 1)
            await event_bus_subscribers._refresh_session_content_projection(BusEvent(
                type="event_journal.written",
                root_id="root",
                sid="root",
                msg_id="msg",
                payload={"seq": 2, "event_type": "agent_message", "source": "event_bus"},
                persist=False,
            ))
            await event_bus_subscribers._refresh_session_content_projection(BusEvent(
                type="event_journal.written",
                root_id="other",
                sid="other",
                msg_id="msg",
                payload={"seq": 1, "event_type": "agent_message", "source": "event_bus"},
                persist=False,
            ))
            assert ticks > 0
            assert dirty_roots == ["other"]
            release.set()
            await ticker_task

        asyncio.run(run())
        drainer.barrier("root")
        assert applied_seqs == [1, 2]
        assert all(name.startswith("session-projection-") for name in applying_threads)
    finally:
        event_bus_subscribers._SESSION_PROJECTION_DISPATCHER = original
        release.set()
        drainer.barrier("root")
        drainer.shutdown()


def test_stub_invalidated_broadcast_is_batched() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    flush_start = source.index("def _flush_stub_invalidated(")
    emit_start = source.index("def _emit_stub_invalidated(", flush_start)
    emit_end = source.index("def _reconcile_catchup_state(", emit_start)
    flush_source = source[flush_start:emit_start]
    emit_source = source[emit_start:emit_end]
    assert 'broadcast_global("stub_invalidated", {"changes": changes})' in flush_source
    assert "_stub_invalidated_pending.extend(changes)" in emit_source
    assert 'broadcast_global("stub_invalidated", ch)' not in flush_source + emit_source
    assert "for ch in changes:" not in flush_source + emit_source


def test_session_search_sqlite_connection_cache_sizes_are_bounded() -> None:
    import importlib
    import os
    import tempfile

    original_home = os.environ.get("BETTER_AGENT_HOME")
    index = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["BETTER_AGENT_HOME"] = tmp
            import session_search_index

            index = importlib.reload(session_search_index)
            writer = index._connect()
            try:
                writer_cache_size = writer.execute("PRAGMA cache_size").fetchone()[0]
            finally:
                writer.close()
            readonly = index._readonly_connection()
            assert readonly is not None
            readonly_cache_size = readonly.execute("PRAGMA cache_size").fetchone()[0]
    finally:
        if index is not None:
            index._close_readonly_connection()
            with index._lock:
                index._close_writer_connection_locked()
        if original_home is None:
            os.environ.pop("BETTER_AGENT_HOME", None)
        else:
            os.environ["BETTER_AGENT_HOME"] = original_home
        if index is not None:
            importlib.reload(index)

    assert writer_cache_size == -200_000
    assert readonly_cache_size == -8_192


def test_native_transcript_sqlite_readonly_connections_use_bounded_cache() -> None:
    import native_transcript_index as index
    import tempfile

    original_home_resolver = index._home_resolver
    with tempfile.TemporaryDirectory() as tmp:
        index.set_home_resolver(lambda: Path(tmp))
        db_path = index._db_path()
        writer = index._connect(db_path, readonly=False)
        try:
            writer_cache_size = writer.execute("PRAGMA cache_size").fetchone()[0]
        finally:
            writer.close()
        readonly = index._readonly_connection()
        try:
            readonly_cache_size = readonly.execute("PRAGMA cache_size").fetchone()[0]
        finally:
            index._close_readonly_connection()
            index.set_home_resolver(original_home_resolver)

    assert writer_cache_size == -200_000
    assert readonly_cache_size == -8_192


def test_build_assistant_msg_skips_same_session_lookup() -> None:
    import orchestrator
    import orchs

    session = {"id": "same-session", "orchestration_mode": "native"}
    with (
        mock.patch.object(orchestrator.session_manager, "get") as get_session,
        mock.patch.object(orchs, "get_strategy", side_effect=_ModeEchoStrategy),
    ):
        result = orchestrator.Coordinator._build_assistant_msg(
            object(),
            session=session,
            app_session_id="same-session",
        )

    assert result == {"mode": "native"}
    get_session.assert_not_called()


def test_build_assistant_msg_uses_app_session_for_cross_session_mode() -> None:
    import orchestrator
    import orchs

    session = {"id": "worker-session", "orchestration_mode": "native"}
    app_session = {"id": "app-session", "orchestration_mode": "supervisor"}
    with (
        mock.patch.object(
            orchestrator.session_manager,
            "get",
            return_value=app_session,
        ) as get_session,
        mock.patch.object(orchs, "get_strategy", side_effect=_ModeEchoStrategy),
    ):
        result = orchestrator.Coordinator._build_assistant_msg(
            object(),
            session=session,
            app_session_id="app-session",
        )

    assert result == {"mode": "supervisor"}
    get_session.assert_called_once_with("app-session")


def test_provider_run_process_poll_runs_off_loop() -> None:
    provider_source = (ROOT / "provider.py").read_text(encoding="utf-8")
    assert "async def popen_poll_off_loop(popen: Any) -> Optional[int]:" in provider_source
    assert "run_in_executor(_PROVIDER_POLL_EXECUTOR, popen.poll)" in provider_source
    assert "async def popen_is_running_off_loop(popen: Any) -> bool:" in provider_source
    helper_start = provider_source.index("    async def is_running_off_loop(")
    helper_end = provider_source.index("    def cancel_all(", helper_start)
    helper_source = provider_source[helper_start:helper_end]
    assert "return await popen_is_running_off_loop(rs.popen)" in helper_source
    assert "rs = self._runs.get(run_id)" in helper_source

    turn_source = (ROOT / "turn_manager.py").read_text(encoding="utf-8")
    drive_start = turn_source.index("    async def _drive_cli_run(")
    drive_source = turn_source[drive_start:]
    assert "await provider.is_running_off_loop(run_id)" in drive_source
    assert "provider_running = await provider.is_running_off_loop(run_id)" in drive_source
    assert "provider.is_running(run_id)" not in drive_source

    watcher_ranges = {
        "provider_claude.py": (
            ("async def _bootstrap_run(", "# 2) Handle the \"runner died"),
            ("async def _watch_complete(", "# ------------------------------------------------------------------\n    # _watch_process_exit"),
            ("async def _watch_process_exit(", "# ------------------------------------------------------------------\n    # _emit_complete_from_file"),
        ),
        "provider_codex.py": (
            ("async def _bootstrap_run(", "if runner_state is None:"),
            ("async def _watch_complete(", "async def _emit_complete_from_file("),
        ),
        "provider_gemini.py": (
            ("async def _bootstrap_run(", "if runner_state is None:"),
            ("async def _watch_complete(", "# ------------------------------------------------------------------\n    # _emit_complete_from_file"),
        ),
        "provider_openai.py": (
            ("async def _bootstrap_run(", "if runner_state is None:"),
            ("async def _watch_complete(", "# ------------------------------------------------------------------\n    # _emit_complete_from_file"),
        ),
    }
    for filename, ranges in watcher_ranges.items():
        source = (ROOT / filename).read_text(encoding="utf-8")
        assert "popen_is_running_off_loop" in source
        for start_marker, end_marker in ranges:
            start = source.index(start_marker)
            end = source.index(end_marker, start)
            watcher_source = source[start:end]
            assert "await popen_is_running_off_loop(rs.popen)" in watcher_source
            assert "rs.popen.poll()" not in watcher_source


def test_jsonl_line_count_uses_fingerprint_cache() -> None:
    import orchs.jsonl_helpers as helpers

    original_open = Path.open
    open_calls = 0

    def counting_open(self, *args, **kwargs):
        nonlocal open_calls
        open_calls += 1
        return original_open(self, *args, **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "session.jsonl"
        path.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
        helpers._JSONL_LINE_COUNT_CACHE.clear()  # type: ignore[attr-defined]

        first = helpers.count_jsonl_lines(path)
        with mock.patch.object(Path, "open", counting_open):
            second = helpers.count_jsonl_lines(path)
        path.write_text('{"a":1}\n{"a":2}\n{"a":3}\n', encoding="utf-8")
        with mock.patch.object(Path, "open", counting_open):
            third = helpers.count_jsonl_lines(path)

    assert first == 2
    assert second == 2
    assert third == 3
    assert open_calls == 1


def test_jsonl_line_count_singleflights_concurrent_cold_reads() -> None:
    import orchs.jsonl_helpers as helpers

    original_open = Path.open
    open_calls = 0
    entered = threading.Event()
    release = threading.Event()

    def counting_open(self, *args, **kwargs):
        nonlocal open_calls
        open_calls += 1
        entered.set()
        release.wait(timeout=5)
        return original_open(self, *args, **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "session.jsonl"
        path.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
        helpers._JSONL_LINE_COUNT_CACHE.clear()  # type: ignore[attr-defined]
        helpers._JSONL_LINE_COUNT_INFLIGHT.clear()  # type: ignore[attr-defined]

        def run() -> int:
            return helpers.count_jsonl_lines(path)

        with mock.patch.object(Path, "open", counting_open):
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(run) for _ in range(8)]
                assert entered.wait(timeout=5)
                release.set()
                results = [future.result(timeout=5) for future in futures]

    assert results == [2] * 8
    assert open_calls == 1


def test_models_catalog_uses_fingerprinted_in_process_cache() -> None:
    source = (ROOT / "models.py").read_text(encoding="utf-8")
    assert "_cache_by_path: dict[Path, tuple[tuple[int, int], dict]] = {}" in source

    read_start = source.index("def _read_cache(")
    read_end = source.index("def _update_cache(", read_start)
    read_source = source[read_start:read_end]
    assert "fingerprint = (stat.st_mtime_ns, stat.st_size)" in read_source
    assert "cached = _cache_by_path.get(path)" in read_source
    assert "return copy.deepcopy(cached[1])" in read_source
    assert "_cache_by_path[path] = (fingerprint, copy.deepcopy(data))" in read_source

    update_start = source.index("def _update_cache(")
    update_end = source.index("def _merge_retired(", update_start)
    update_source = source[update_start:update_end]
    assert "_cache_by_path[path] = ((stat.st_mtime_ns, stat.st_size), copy.deepcopy(cur))" in update_source

    helper_start = source.index("def _read_catalog_models(")
    helper_end = source.index("def _models_for(", helper_start)
    helper_source = source[helper_start:helper_end]
    assert "-> tuple[list[str], list[str], bool, dict | None]:" in helper_source
    assert "return models, cached_retired, has_cache, cached" in helper_source

    catalog_start = source.index("def models_catalog(")
    catalog_end = source.index("async def refresh_one(", catalog_start)
    catalog_source = source[catalog_start:catalog_end]
    assert "models, retired, has_cache, cached = _read_catalog_models(rec)" in catalog_source
    after_helper = catalog_source.split("models, retired, has_cache, cached = _read_catalog_models(rec)", 1)[1]
    assert "_read_cache(" not in after_helper


def test_queue_projection_upsert_backgrounds_from_event_loop() -> None:
    import session_queue_projection
    from session_manager import manager as session_manager

    async def run() -> None:
        with (
            mock.patch.object(session_queue_projection, "upsert_record") as sync_upsert,
            mock.patch("session_manager._submit_queue_projection_record") as background_upsert,
        ):
            session_manager._upsert_queue_record({"id": "event-loop-session"})

        sync_upsert.assert_not_called()
        background_upsert.assert_called_once_with({"id": "event-loop-session"})

    asyncio.run(run())


def test_queue_projection_lock_never_blocks_event_loop() -> None:
    import session_queue_projection
    import session_manager as manager_module

    async def run() -> None:
        lock_acquired = threading.Event()
        release_lock = threading.Event()

        def hold_projection_lock() -> None:
            with session_queue_projection._lock:
                lock_acquired.set()
                release_lock.wait(timeout=5)

        holder = threading.Thread(target=hold_projection_lock)
        holder.start()
        await asyncio.to_thread(lock_acquired.wait, 2)
        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            for _ in range(5):
                await asyncio.sleep(0.02)
                ticks += 1

        manager_module.manager._upsert_queue_record({"id": "locked-projection"})
        await heartbeat()
        assert ticks == 5
        release_lock.set()
        holder.join(timeout=2)
        await asyncio.to_thread(manager_module.drain_queue_projection_submissions)
        assert session_queue_projection.flush_pending_writes(timeout=5)
        assert session_queue_projection.get("locked-projection") == {
            "id": "locked-projection",
        }

    asyncio.run(run())


def test_queue_projection_submission_coalesces_and_shutdown_rejection_is_dirty() -> None:
    import session_manager as manager_module

    original_upsert = manager_module._upsert_queue_projection_record
    started = threading.Event()
    release = threading.Event()

    def blocked_upsert(_record: dict) -> None:
        started.set()
        release.wait(timeout=5)

    manager_module._upsert_queue_projection_record = blocked_upsert
    try:
        manager_module._submit_queue_projection_record({"id": "busy", "value": 0})
        assert started.wait(timeout=2)
        for value in range(100):
            manager_module._submit_queue_projection_record({"id": "busy", "value": value})
        with manager_module._queue_projection_cv:
            assert list(manager_module._queue_projection_pending) == ["busy"]
            assert manager_module._queue_projection_pending["busy"]["value"] == 99
        release.set()
        manager_module.drain_queue_projection_submissions()

        with manager_module._queue_projection_cv:
            manager_module._queue_projection_accepting = False
        manager_module._submit_queue_projection_record({"id": "late"})
        try:
            manager_module.drain_queue_projection_submissions()
        except RuntimeError:
            pass
        else:
            raise AssertionError("shutdown-rejected projection did not fail drain")
    finally:
        release.set()
        manager_module._upsert_queue_projection_record = original_upsert
        with manager_module._queue_projection_cv:
            manager_module._queue_projection_accepting = True
            manager_module._queue_projection_failure = None
            manager_module._queue_projection_pending.clear()


def test_queue_projection_certification_rejects_late_mutations() -> None:
    import session_queue_projection

    generation = session_queue_projection.certification_generation()
    session_queue_projection.mark_dirty()
    assert not session_queue_projection.mark_current_if_generation(generation)
    current_generation = session_queue_projection.certification_generation()
    assert session_queue_projection.mark_current_if_generation(current_generation)
    assert session_queue_projection.projection_is_current()
    session_queue_projection.mark_dirty()
    assert not session_queue_projection.projection_is_current()


def test_provider_spawn_flushes_only_target_root() -> None:
    import session_manager as manager_module

    manager = manager_module.manager
    suffix = str(time.time_ns())
    target = f"target-root-{suffix}"
    unrelated = f"unrelated-root-{suffix}"
    target_written = threading.Event()
    unrelated_release = threading.Event()
    original_write = manager_module.session_store.write_session_full
    original_roots = manager._roots
    original_node_roots = manager._node_root_id

    def write(root: dict, **_kwargs) -> None:
        if root["id"] == unrelated:
            unrelated_release.wait(timeout=5)
        if root["id"] == target:
            target_written.set()

    try:
        manager._lock_for_root(target)
        manager._lock_for_root(unrelated)
        manager._roots = {
            target: {"id": target, "messages": []},
            unrelated: {"id": unrelated, "messages": []},
        }
        manager._node_root_id = {target: target, unrelated: unrelated}
        manager_module.session_store.write_session_full = write
        with manager_module._persist_state_lock:
            manager_module._cancel_persist_deadline_unlocked(target)
            manager_module._cancel_persist_deadline_unlocked(unrelated)
            manager_module._persist_inflight.discard(target)
            manager_module._persist_inflight.discard(unrelated)
            manager_module._persist_pending[target] = manager._roots[target]
            manager_module._persist_pending[unrelated] = manager._roots[unrelated]
        started = time.perf_counter()
        manager.flush_root_persist(target)
        assert time.perf_counter() - started < 0.5
        assert target_written.is_set()
        with manager_module._persist_state_lock:
            assert unrelated in manager_module._persist_pending
    finally:
        unrelated_release.set()
        manager_module.session_store.write_session_full = original_write
        with manager_module._persist_state_lock:
            manager_module._persist_pending.pop(target, None)
            manager_module._persist_pending.pop(unrelated, None)
            manager_module._persist_inflight.discard(target)
            manager_module._persist_inflight.discard(unrelated)
        manager._roots = original_roots
        manager._node_root_id = original_node_roots


def test_queue_projection_rebuild_retries_concurrent_upsert() -> None:
    import session_queue_projection
    import session_store

    original_files = session_queue_projection._session_files_fingerprint
    original_session_files = session_store._session_json_files
    scan_started = threading.Event()
    release_scan = threading.Event()
    scan_calls = 0

    def session_files() -> list:
        nonlocal scan_calls
        scan_calls += 1
        scan_started.set()
        release_scan.wait(timeout=5)
        return []

    try:
        session_queue_projection._session_files_fingerprint = lambda: {}
        session_store._session_json_files = session_files
        with session_queue_projection._lock:
            session_queue_projection._loaded = True
            session_queue_projection._records.clear()
        thread = threading.Thread(target=session_queue_projection.rebuild_from_disk)
        thread.start()
        assert scan_started.wait(timeout=2)
        session_queue_projection.upsert_record({"id": "concurrent", "value": 2})
        release_scan.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert scan_calls == 1
        assert session_queue_projection.flush_pending_writes(timeout=5)
        assert session_queue_projection.get("concurrent") == {"id": "concurrent", "value": 2}
        # The rebuild snapshot covers the concurrent upsert's sequence, so the
        # replay-overlay entry must be pruned instead of leaking forever.
        with session_queue_projection._lock:
            assert "concurrent" not in session_queue_projection._mutation_log
    finally:
        release_scan.set()
        session_queue_projection._session_files_fingerprint = original_files
        session_store._session_json_files = original_session_files


def test_flush_root_persist_waits_for_same_root_inflight() -> None:
    import session_manager as manager_module

    manager = manager_module.manager
    root_id = f"same-root-{time.time_ns()}"
    manager._lock_for_root(root_id)
    written = threading.Event()
    finished = threading.Event()
    original_write = manager_module.session_store.write_session_full
    manager_module.session_store.write_session_full = lambda *_args, **_kwargs: written.set()
    try:
        with manager_module._persist_state_changed:
            manager_module._persist_inflight.add(root_id)
            manager_module._persist_pending[root_id] = {"id": root_id, "messages": []}
        thread = threading.Thread(
            target=lambda: (manager.flush_root_persist(root_id), finished.set()),
        )
        thread.start()
        assert not finished.wait(timeout=0.1)
        with manager_module._persist_state_changed:
            manager_module._persist_inflight.discard(root_id)
            manager_module._persist_state_changed.notify_all()
        assert finished.wait(timeout=2)
        assert written.is_set()
        thread.join(timeout=2)
    finally:
        manager_module.session_store.write_session_full = original_write
        with manager_module._persist_state_changed:
            manager_module._persist_pending.pop(root_id, None)
            manager_module._persist_inflight.discard(root_id)
            manager_module._persist_state_changed.notify_all()


def test_flush_root_persist_requeues_claim_after_write_failure() -> None:
    import session_manager as manager_module

    manager = manager_module.manager
    root_id = f"retry-root-{time.time_ns()}"
    manager._lock_for_root(root_id)
    attempts = 0
    original_write = manager_module.session_store.write_session_full

    def write(*_args, **_kwargs) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected first write failure")

    try:
        manager_module.session_store.write_session_full = write
        with manager_module._persist_state_changed:
            manager_module._persist_pending[root_id] = {"id": root_id, "messages": []}
        try:
            manager.flush_root_persist(root_id)
        except OSError:
            pass
        else:
            raise AssertionError("failed durability write did not fail closed")
        with manager_module._persist_state_changed:
            assert root_id in manager_module._persist_pending
        manager.flush_root_persist(root_id)
        assert attempts == 2
        with manager_module._persist_state_changed:
            assert root_id not in manager_module._persist_pending
    finally:
        manager_module.session_store.write_session_full = original_write
        with manager_module._persist_state_changed:
            manager_module._persist_pending.pop(root_id, None)
            manager_module._persist_inflight.discard(root_id)
            manager_module._persist_state_changed.notify_all()


def test_projection_delete_records_removes_nested_subtree_atomically() -> None:
    import session_queue_projection

    ids = [f"projection-subtree-{time.time_ns()}-{index}" for index in range(3)]
    with session_queue_projection._lock:
        session_queue_projection._loaded = True
        for index, sid in enumerate(ids):
            session_queue_projection._records[sid] = {"id": sid, "value": index}
    session_queue_projection.delete_records(ids)
    assert session_queue_projection.get_many(ids) == {}
    with session_queue_projection._write_cv:
        assert not set(ids) & set(session_queue_projection._pending_writes)
    manager_source = (ROOT / "session_manager.py").read_text(encoding="utf-8")
    assert manager_source.count("session_queue_projection.delete_records(") == 1
    assert "session_queue_projection.delete_records(deleted_sids)" in manager_source


def _projection_db_rows(ids: list[str]) -> set[str]:
    import sqlite3

    import session_queue_projection

    path = session_queue_projection._database_path()
    if not path.exists():
        return set()
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        marks = ",".join("?" for _ in ids)
        return {
            row[0]
            for row in connection.execute(
                f"SELECT id FROM records WHERE id IN ({marks})", list(ids),
            )
        }
    finally:
        connection.close()


def test_evicted_root_delete_removes_all_nested_projection_state() -> None:
    import session_manager as manager_module
    import session_queue_projection

    manager = manager_module.manager
    root = manager.create(name="evicted-root", cwd=str(ROOT), orchestration_mode="native")
    manager.set_agent_sid(root["id"], "native", f"agent-{time.time_ns()}")
    child = manager.fork(root["id"], name="child")
    manager.set_agent_sid(child["id"], "native", f"agent-{time.time_ns()}")
    grandchild = manager.fork(child["id"], name="grandchild")
    ids = [root["id"], child["id"], grandchild["id"]]
    for sid in ids:
        session_queue_projection.upsert_record({"id": sid, "queued_prompts": []})
    assert _projection_db_rows(ids) == set(ids)
    manager._roots.pop(root["id"], None)
    assert manager.delete(root["id"])
    assert session_queue_projection.get_many(ids) == {}
    assert _projection_db_rows(ids) == set()
    with session_queue_projection._write_cv:
        assert not set(ids) & set(session_queue_projection._pending_writes)


def test_queue_projection_rebuild_concurrent_delete_wins() -> None:
    import session_queue_projection
    import session_store

    sid = f"deleted-during-rebuild-{time.time_ns()}"
    scan_started = threading.Event()
    release_scan = threading.Event()
    original_session_files = session_store._session_json_files
    original_fingerprint = session_queue_projection._session_files_fingerprint

    def session_files() -> list:
        scan_started.set()
        release_scan.wait(timeout=5)
        return []

    try:
        session_store._session_json_files = session_files
        session_queue_projection._session_files_fingerprint = lambda: {}
        with session_queue_projection._lock:
            session_queue_projection._loaded = True
            session_queue_projection._records[sid] = {"id": sid, "value": 1}
        thread = threading.Thread(target=session_queue_projection.rebuild_from_disk)
        thread.start()
        assert scan_started.wait(timeout=2)
        session_queue_projection.delete_record(sid)
        release_scan.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert session_queue_projection.flush_pending_writes(timeout=5)
        assert session_queue_projection.get(sid) is None
        assert not _projection_db_rows([sid])
        with session_queue_projection._lock:
            assert sid not in session_queue_projection._mutation_log
    finally:
        release_scan.set()
        session_store._session_json_files = original_session_files
        session_queue_projection._session_files_fingerprint = original_fingerprint


def test_shutdown_global_drain_flushes_all_and_certifies_exact_generation() -> None:
    import session_manager as manager_module
    import session_queue_projection

    manager = manager_module.manager
    roots = [f"shutdown-root-{time.time_ns()}-{i}" for i in range(2)]
    for root_id in roots:
        manager._lock_for_root(root_id)
    writes: list[str] = []
    original_write = manager_module.session_store.write_session_full
    original_fingerprint = session_queue_projection._session_files_fingerprint
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    shutdown_source = main_source[main_source.index("async def on_shutdown()") :]
    assert shutdown_source.index("session_manager.flush_pending_persists()") < shutdown_source.index(
        "await asyncio.to_thread(drain_queue_projection_submissions)"
    ) < shutdown_source.index("session_queue_projection.flush_pending_writes(timeout=5.0)")
    assert "mark_current_if_generation(\n                    certification_generation" in shutdown_source
    try:
        manager_module.session_store.write_session_full = (
            lambda root, **_kwargs: writes.append(root["id"])
        )
        with manager_module._persist_state_changed:
            for root_id in roots:
                manager_module._persist_pending[root_id] = {"id": root_id, "messages": []}
        manager.flush_pending_persists()
        assert set(writes) == set(roots)
        with manager_module._persist_state_changed:
            assert not set(roots) & (
                set(manager_module._persist_pending) | set(manager_module._persist_inflight)
            )
        session_queue_projection._session_files_fingerprint = lambda: {}
        generation = session_queue_projection.certification_generation()
        assert session_queue_projection.mark_current_if_generation(generation)
        session_queue_projection.mark_dirty()
        assert not session_queue_projection.mark_current_if_generation(generation)
    finally:
        manager_module.session_store.write_session_full = original_write
        session_queue_projection._session_files_fingerprint = original_fingerprint
        with manager_module._persist_state_changed:
            for root_id in roots:
                manager_module._persist_pending.pop(root_id, None)
                manager_module._persist_inflight.discard(root_id)
            manager_module._persist_state_changed.notify_all()


def test_queue_projection_upsert_stays_inline_without_event_loop() -> None:
    import session_queue_projection
    from session_manager import manager as session_manager

    with (
        mock.patch.object(session_queue_projection, "upsert_record") as sync_upsert,
        mock.patch.object(
            session_queue_projection,
            "upsert_record_background",
        ) as background_upsert,
    ):
        session_manager._upsert_queue_record({"id": "sync-session"})

    sync_upsert.assert_called_once_with({"id": "sync-session"})
    background_upsert.assert_not_called()


def test_queue_projection_background_upsert_latest_wins() -> None:
    import session_queue_projection

    assert session_queue_projection.flush_pending_writes(timeout=5)
    with session_queue_projection._write_cv:
        session_queue_projection._pending_writes.clear()
    with session_queue_projection._lock:
        original_loaded = session_queue_projection._loaded
        original_records = dict(session_queue_projection._records)
        session_queue_projection._loaded = True
        session_queue_projection._records.clear()

    writes: list[dict] = []

    def record_batch(batch: dict) -> None:
        for _sid, (_sequence, record) in batch.items():
            if record is not None:
                writes.append(dict(record))

    try:
        with mock.patch.object(
            session_queue_projection,
            "_compact_batch",
            side_effect=record_batch,
        ):
            session_queue_projection.upsert_record_background({
                "id": "latest-session",
                "value": 1,
            })
            session_queue_projection.upsert_record_background({
                "id": "latest-session",
                "value": 2,
            })
            assert session_queue_projection.flush_pending_writes(timeout=5)

        assert writes
        assert writes[-1] == {"id": "latest-session", "value": 2}
        assert session_queue_projection.get("latest-session") == {
            "id": "latest-session",
            "value": 2,
        }
    finally:
        assert session_queue_projection.flush_pending_writes(timeout=5)
        with session_queue_projection._write_cv:
            session_queue_projection._pending_writes.clear()
        with session_queue_projection._lock:
            session_queue_projection._journal.pop("latest-session", None)
            session_queue_projection._mutation_log.pop("latest-session", None)
            session_queue_projection._records.clear()
            session_queue_projection._records.update(original_records)
            session_queue_projection._loaded = original_loaded


def test_queue_projection_slow_writer_does_not_block_event_loop_upsert() -> None:
    import session_manager as manager_module
    import session_queue_projection
    from session_manager import manager as session_manager

    assert session_queue_projection.flush_pending_writes(timeout=5)
    with session_queue_projection._write_cv:
        session_queue_projection._pending_writes.clear()
    with session_queue_projection._lock:
        original_loaded = session_queue_projection._loaded
        original_records = dict(session_queue_projection._records)
        session_queue_projection._loaded = True
        session_queue_projection._records.clear()

    started = threading.Event()
    release = threading.Event()
    done = threading.Event()
    errors: list[BaseException] = []
    writes: list[dict] = []

    def slow_batch(batch: dict) -> None:
        started.set()
        if not release.wait(timeout=5):
            raise TimeoutError("slow queue projection write was not released")
        for _sid, (_sequence, record) in batch.items():
            if record is not None:
                writes.append(dict(record))

    async def event_loop_upsert() -> None:
        session_manager._upsert_queue_record({
            "id": "slow-writer-session",
            "value": 2,
        })

    def run_event_loop_upsert() -> None:
        try:
            asyncio.run(event_loop_upsert())
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    try:
        with mock.patch.object(
            session_queue_projection,
            "_compact_batch",
            side_effect=slow_batch,
        ):
            session_queue_projection.upsert_record_background({
                "id": "slow-writer-session",
                "value": 1,
            })
            assert started.wait(timeout=5)
            thread = threading.Thread(target=run_event_loop_upsert)
            thread.start()
            assert done.wait(timeout=0.5)
            release.set()
            thread.join(timeout=5)
            manager_module.drain_queue_projection_submissions()
            assert session_queue_projection.flush_pending_writes(timeout=5)

        assert not errors
        assert writes[-1] == {"id": "slow-writer-session", "value": 2}
    finally:
        release.set()
        assert session_queue_projection.flush_pending_writes(timeout=5)
        with session_queue_projection._write_cv:
            session_queue_projection._pending_writes.clear()
        with session_queue_projection._lock:
            session_queue_projection._journal.pop("slow-writer-session", None)
            session_queue_projection._mutation_log.pop("slow-writer-session", None)
            session_queue_projection._records.clear()
            session_queue_projection._records.update(original_records)
            session_queue_projection._loaded = original_loaded


def test_startup_does_not_shadow_extension_store_import() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    module_symbols = symtable.symtable(source, str(ROOT / "main.py"), "exec")
    startup_symbols = next(
        child for child in module_symbols.get_children() if child.get_name() == "on_startup"
    )
    if "extension_store" not in startup_symbols.get_identifiers():
        return
    extension_store_symbol = startup_symbols.lookup("extension_store")
    assert not extension_store_symbol.is_local()


def test_user_input_file_store_calls_are_off_loop() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    state_start = source.index("async def _broadcast_user_input_state(")
    state_end = source.index("@app.get(\"/api/user-input/pending\")", state_start)
    state_source = source[state_start:state_end]
    assert "pending_count = await asyncio.to_thread(" in state_source
    assert "user_input_store.pending_count_for_session" in state_source
    assert "\"pending_user_input_count\": pending_count" in state_source

    pending_start = source.index("async def get_pending_user_inputs(")
    pending_end = source.index("@app.post(\"/api/user-input/{request_id}/resolve\")", pending_start)
    pending_source = source[pending_start:pending_end]
    assert "await asyncio.to_thread(" in pending_source
    assert "user_input_store.pending_for_session" in pending_source
    assert "user_input_store.pending_for_session(sid)" not in pending_source

    resolve_start = source.index("async def resolve_user_input(")
    resolve_end = source.index("@app.post(\"/api/user-input/{request_id}/cancel\")", resolve_start)
    resolve_source = source[resolve_start:resolve_end]
    assert "await asyncio.to_thread(user_input_store.get_request, request_id)" in resolve_source
    assert "await asyncio.to_thread(\n        user_input_store.resolve_request" in resolve_source
    assert "user_input_store.resolve_request(request_id, answers)" not in resolve_source

    cancel_start = source.index("async def cancel_user_input(")
    cancel_end = source.index("@app.post(\"/api/internal/user-input/request\")", cancel_start)
    cancel_source = source[cancel_start:cancel_end]
    assert "await asyncio.to_thread(user_input_store.get_request, request_id)" in cancel_source
    assert "await asyncio.to_thread(user_input_store.cancel_request, request_id)" in cancel_source
    assert "user_input_store.cancel_request(request_id)" not in cancel_source

    internal_start = source.index("async def internal_request_user_input(")
    internal_end = source.index("@app.post(\"/api/internal/open-config-panel\")", internal_start)
    internal_source = source[internal_start:internal_end]
    assert "public_req, created = await asyncio.to_thread(" in internal_source
    assert "user_input_store.create_or_get_pending_request" in internal_source
    assert "user_input_store.create_or_get_pending_request(" not in internal_source.replace(
        "user_input_store.create_or_get_pending_request,\n",
        "",
    )


def test_session_reconcile_uses_dedicated_executor_with_context() -> None:
    source = (ROOT / "session_manager.py").read_text(encoding="utf-8")
    assert "def _new_reconcile_executor()" in source
    assert "def reopen_reconciles() -> None:" in source
    assert "max_workers=2," in source
    assert "thread_name_prefix=\"session-reconcile\"" in source
    assert "async def shutdown_reconciles() -> None:" in source
    assert "manager._reconcile_accepting = False" in source
    reconcile_start = source.index("    async def _async_reconcile_with_progress(")
    reconcile_end = source.index("    def _emit_processing(", reconcile_start)
    reconcile_source = source[reconcile_start:reconcile_end]
    assert "asyncio.to_thread(self._sync_reconcile, root_id)" not in reconcile_source
    assert "contextvars.copy_context()" in reconcile_source
    assert "run_in_executor(\n            _RECONCILE_EXECUTOR" in reconcile_source
    assert "session.reconcile.queue_wait" in reconcile_source
    assert "session.reconcile.total" in reconcile_source
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "shutdown_reconciles" in main_source
    shutdown_start = main_source.index("async def on_shutdown()")
    shutdown_end = main_source.index("# Internal Endpoints", shutdown_start)
    shutdown_source = main_source[shutdown_start:shutdown_end]
    assert "await shutdown_reconciles()" in shutdown_source

    import session_manager as sm

    observed: dict[str, object] = {}
    marker = contextvars.ContextVar("reconcile_marker")

    class _ReconcileHarness:
        _emit_reconciled_fn = None
        _emit_stub_invalidated_fn = None

        async def _async_reconcile_with_progress(self, root_id: str) -> None:
            return await sm.SessionManager._async_reconcile_with_progress(self, root_id)

        def _sync_reconcile(self, root_id: str) -> list:
            observed["root_id"] = root_id
            observed["thread_name"] = threading.current_thread().name
            observed["marker"] = marker.get(None)
            return []

        def _emit_processing(self, kind: str, root_id: str) -> None:
            observed.setdefault("processing", []).append((kind, root_id))

    async def _run() -> None:
        token = marker.set("ctx-ok")
        try:
            await _ReconcileHarness()._async_reconcile_with_progress("root-x")
        finally:
            marker.reset(token)

    asyncio.run(_run())
    assert observed["root_id"] == "root-x"
    assert str(observed["thread_name"]).startswith("session-reconcile")
    assert observed["marker"] == "ctx-ok"


def test_session_search_rebuild_streams_insert_batches() -> None:
    source = (ROOT / "session_search_index.py").read_text(encoding="utf-8")
    start = source.index("def rebuild_from_disk()")
    end = source.index("def _index_file_rows(", start)
    rebuild_source = source[start:end]
    row_start = source.index("def _index_file_rows(")
    row_end = source.find("\ndef ", row_start + 1)
    if row_end == -1:
        row_end = len(source)
    row_source = source[row_start:row_end]
    assert "_REBUILD_INSERT_BATCH_SIZE = 1000" in source
    assert "batch: list[tuple[str, str]] = []" in rebuild_source
    assert "_insert_index_rows(conn, batch)" in rebuild_source
    assert "rows.extend(" not in rebuild_source
    assert "yield (sid, text)" in row_source
    assert "rows: list" not in row_source
    assert ".append(" not in row_source


def test_session_search_index_file_rows_are_consumed_incrementally() -> None:
    session_search_index = importlib.import_module("session_search_index")
    original_batch_size = session_search_index._REBUILD_INSERT_BATCH_SIZE
    original_insert = session_search_index._insert_index_rows
    session_search_index._REBUILD_INSERT_BATCH_SIZE = 2
    inserted_batches: list[list[tuple[str, str]]] = []
    lines_read_at_insert: list[int] = []

    class CountingPath:
        def __init__(self, lines: list[str]) -> None:
            self.lines = lines
            self.lines_read = 0

        def open(self, *args, **kwargs):
            owner = self

            class Handle:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def __iter__(self):
                    return self

                def __next__(self):
                    if owner.lines_read >= len(owner.lines):
                        raise StopIteration
                    line = owner.lines[owner.lines_read]
                    owner.lines_read += 1
                    return line

            return Handle()

    def event(text: str) -> str:
        return json.dumps(
            {
                "type": "agent_message",
                "data": {
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}],
                    }
                },
            }
        ) + "\n"

    path = CountingPath([event("one"), event("two"), event("three")])

    def fake_insert(conn, rows: list[tuple[str, str]]) -> None:
        inserted_batches.append(list(rows))
        lines_read_at_insert.append(path.lines_read)

    try:
        session_search_index._insert_index_rows = fake_insert
        session_search_index._index_file(object(), "sid", path)
    finally:
        session_search_index._insert_index_rows = original_insert
        session_search_index._REBUILD_INSERT_BATCH_SIZE = original_batch_size

    assert [len(batch) for batch in inserted_batches] == [2, 1]
    assert lines_read_at_insert == [2, 3]


def test_session_detail_cache_hit_validation_uses_cheap_fingerprint() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    manager_source = (ROOT / "session_manager.py").read_text(encoding="utf-8")
    helper_start = source.index("def _session_detail_cached_key_still_current(")
    helper_end = source.index("def _floor_events_from_seq(", helper_start)
    helper_source = source[helper_start:helper_end]
    assert "session_manager._root_id_for(session_id)" not in helper_source
    assert "cached_tree_key = key[1]" in helper_source
    assert "session_manager.root_tree_stub_cache_key_for_root(" in helper_source
    assert "_session_event_meta(root_id)" in helper_source
    assert "_session_detail_watermarks(" in helper_source
    assert "_session_detail_response_cache_key_sync(" not in helper_source
    # `_session_event_meta` must stay cheap on the cache-hit path: it may only
    # fall through to the event reader when the stat fingerprint changed.
    meta_start = source.index("def _session_event_meta(")
    meta_end = source.index("def _session_detail_watermarks(", meta_start)
    meta_source = source[meta_start:meta_end]
    assert "_session_event_file_fingerprint(root_id)" in meta_source
    assert "if cached is not None and cached[0] == fingerprint:" in meta_source
    assert "known_root_id: Optional[str] = None" in manager_source
    assert "known_root_id=root_id if isinstance(root_id, str) else None" in source
    root_helper_start = manager_source.index("def root_tree_stub_cache_key_for_root(")
    root_helper_end = manager_source.index("def get_message_full(", root_helper_start)
    root_helper_source = manager_source[root_helper_start:root_helper_end]
    assert "self._load_root_impl(root_id, hydrate_events=False)" in root_helper_source
    assert "self._load_root(root_id" not in root_helper_source

    route_start = source.index("async def get_session(")
    route_end = source.index("@app.get(\"/api/sessions/{session_id}/messages\")", route_start)
    route_source = source[route_start:route_end]
    assert "_session_detail_cached_key_still_current" in route_source
    assert "_session_detail_response_cache_key_sync" not in route_source[
        route_source.index("if cached_full_key is not None:"):
        route_source.index("perf.record(\"sessions.detail.response_cache.miss\"",)
    ]
    startup_start = source.index("async def on_startup()")
    startup_end = source.index("async def on_shutdown()", startup_start)
    startup_source = source[startup_start:startup_end]
    assert "startup-session-event-meta-projection-warm" not in startup_source
    assert "session_event_projection_warm" not in startup_source
    assert "_rebuild_session_search_index_if_empty" in startup_source


def test_stubbed_tree_cache_attaches_root_events_after_cache_copy() -> None:
    source = (ROOT / "session_manager.py").read_text(encoding="utf-8")
    build_start = source.index("def _build_stubbed_tree(")
    build_end = source.index("def _compute_messages_snapshot(", build_start)
    build_source = source[build_start:build_end]
    assert "_tree_stub_attached_cache" in source
    assert "root_events_version = self._root_events_version_for_tree(rid)" in build_source
    assert "_tree_stub_attached_cache.get(attached_cache_key)" in build_source
    assert "self._attach_root_events_to_stubbed_tree(tree, rid)" in build_source
    assert build_source.index("tree = _copy_jsonish(cached)") < build_source.index(
        "self._attach_root_events_to_stubbed_tree(tree, rid)"
    )
    assert build_source.index("self._tree_stub_cache[cache_key] = _copy_jsonish(tree)") < build_source.rindex(
        "self._attach_root_events_to_stubbed_tree(tree, rid)"
    )
    assert "self._cache_attached_stubbed_tree(attached_cache_key, tree)" in build_source

    ingester_source = (ROOT / "event_ingester.py").read_text(encoding="utf-8")
    version_start = ingester_source.index("def root_events_version(")
    version_end = ingester_source.index("def _read_all_events_locked(", version_start)
    version_source = ingester_source[version_start:version_end]
    assert "self._scan_max_seq(root_id)" in version_source
    assert "_build_root_events_projection" not in version_source
    assert "_read_all_events_locked" not in version_source


def test_ba_home_memoizes_resolution_off_loop() -> None:
    """`ba_home()` is called hundreds of times (incl. per-request auth) and
    faulthandler dumps showed it as the single most frequent event-loop
    blocking frame, because every call ran mkdir + realpath + chmod syscalls.
    Lock in the memoization: (1) source keeps the env-keyed cache and never
    caches at import; (2) a cache hit issues no filesystem resolve; (3) an env
    swap still re-resolves (test-isolation contract)."""
    source = (ROOT / "paths.py").read_text(encoding="utf-8")
    assert "_HOME_CACHE" in source
    assert "def reset_home_cache(" in source
    assert "def _resolve_home_uncached(" in source
    # The hot fn must consult the cache before resolving.
    fn_start = source.index("def ba_home(")
    fn_end = source.index("\nbc_home = ba_home", fn_start)
    fn_source = source[fn_start:fn_end]
    assert "_HOME_CACHE.get(cache_key)" in fn_source
    assert "os.environ.get(_PRIMARY_HOME_ENV" in fn_source  # key read fresh, not import-cached

    # Behavioral: cache hit must not call the resolver (proves syscall-free).
    import importlib
    import tempfile

    home = tempfile.mkdtemp(prefix="ba-elb-test-")
    prev_primary = os.environ.get("BETTER_AGENT_HOME")
    prev_legacy = os.environ.get("BETTER_CLAUDE_HOME")
    try:
        os.environ["BETTER_AGENT_HOME"] = home
        os.environ["BETTER_CLAUDE_HOME"] = home
        paths = importlib.import_module("paths")
        paths.reset_home_cache()
        first = paths.ba_home()
        sentinel = {"called": False}
        real = paths._resolve_home_uncached

        def _boom():
            sentinel["called"] = True
            raise AssertionError("ba_home cache hit must not re-resolve")

        paths._resolve_home_uncached = _boom
        try:
            again = paths.ba_home()
        finally:
            paths._resolve_home_uncached = real
        assert again == first
        assert sentinel["called"] is False

        # Env swap re-resolves onto the new home.
        other = tempfile.mkdtemp(prefix="ba-elb-test2-")
        os.environ["BETTER_AGENT_HOME"] = other
        os.environ["BETTER_CLAUDE_HOME"] = other
        swapped = paths.ba_home()
        assert str(swapped).startswith(other)
    finally:
        if prev_primary is None:
            os.environ.pop("BETTER_AGENT_HOME", None)
        else:
            os.environ["BETTER_AGENT_HOME"] = prev_primary
        if prev_legacy is None:
            os.environ.pop("BETTER_CLAUDE_HOME", None)
        else:
            os.environ["BETTER_CLAUDE_HOME"] = prev_legacy
        try:
            import importlib as _il
            _il.import_module("paths").reset_home_cache()
        except Exception:
            pass




def test_get_historical_children_get_ref_runs_off_loop() -> None:
    """Regression: `get_historical_children` (main.py) must not call
    `session_manager.get_ref` inline on the event loop thread. `get_ref`
    takes the root's `_lock_for_root`, which a cold render-tree hydration
    (or any other holder) can pin for many seconds — an inline call would
    freeze every other request/websocket on the process for that long.
    Proves the async BEHAVIOR (event loop stays responsive while the lock
    is held elsewhere), not just that the source text changed."""
    import main
    from fastapi import HTTPException
    from session_manager import manager as session_manager

    sess = session_manager.create(
        name="target", cwd="/tmp", orchestration_mode="native",
        model="model", source="test",
    )
    root_id = sess["id"]

    lock_acquired = threading.Event()
    release_lock = threading.Event()
    ticks = 0
    outcome: dict[str, object] = {}

    def hold_root_lock() -> None:
        with session_manager._lock_for_root(root_id):
            lock_acquired.set()
            release_lock.wait(timeout=10)

    async def run() -> None:
        nonlocal ticks

        async def heartbeat() -> None:
            nonlocal ticks
            for _ in range(5):
                await asyncio.sleep(0.02)
                ticks += 1

        call_task = asyncio.create_task(main.get_historical_children(
            session_id=root_id,
            message_id="does-not-exist",
            parent_id="root",
            revision="r1",
            limit=50,
            cursor=None,
        ))
        await heartbeat()
        release_lock.set()
        try:
            await call_task
        except HTTPException as exc:
            outcome["status_code"] = exc.status_code
            outcome["detail"] = exc.detail

    def run_loop() -> None:
        asyncio.run(run())

    holder = threading.Thread(target=hold_root_lock)
    holder.start()
    assert lock_acquired.wait(timeout=2)

    # If get_historical_children blocks the loop thread synchronously on
    # the held lock, NOTHING in that loop can run — including asyncio's
    # own timeout machinery, since it needs the same thread to fire. Only
    # an outside OS-thread wall-clock join can catch that kind of freeze.
    loop_thread = threading.Thread(target=run_loop)
    loop_thread.start()
    loop_thread.join(timeout=3)
    if loop_thread.is_alive():
        release_lock.set()
        loop_thread.join(timeout=5)
        raise AssertionError(
            "event loop froze while get_historical_children awaited "
            "get_ref — it is blocking inline instead of via asyncio.to_thread"
        )
    holder.join(timeout=2)

    assert ticks == 5, (
        f"heartbeat only ticked {ticks}/5 times before get_ref's lock was "
        "released — get_historical_children is not running get_ref "
        "concurrently with other event-loop work"
    )
    assert outcome.get("status_code") == 404, outcome


# Static source-grep regression cases collapsed into one data-driven test.
# Entry: (label, checks). Check kinds:
#   ("grep", file, steps, must, must_not) — containment within the region
#   ("any_in", file, steps, needles) — at least one needle in the region
#   ("ordered", file, steps, paths) — anchor-path positions strictly increase
#   ("count_eq"/"count_ge", file, steps, needle, n) — occurrence count bound
# steps narrow the file text: each (start, end) keeps text from the first
# occurrence of start (file head if None) up to the first following end
# (file tail if None).
SOURCE_GREP_CASES: tuple = (
    ('get_historical_children_reads_root_off_loop', (
     ('grep', 'main.py',
      (('async def get_historical_children(', '\ndef _resolve_session_node_id('),),
      ('node = await asyncio.to_thread(session_manager.get_ref, session_id)',
       'rebuild_root = await asyncio.to_thread(session_manager.get_ref, root_id)',
      ),
      ('node = session_manager.get_ref(session_id)',
       'historical_children_projection.schedule_rebuild(\n            root_id, session_manager.get_ref(root_id), priority=True,',
      ),
     ),
    )),
    ('hook_runner_loads_config_off_loop', (
     ('grep', 'hook_runner.py', (), ('hooks = await asyncio.to_thread(hook_store.list_hooks)',),
      ('hooks = hook_store.list_hooks()',),
     ),
    )),
    ('wire_tailer_gap_fill_reads_journal_off_loop', (
     ('grep', 'jsonl_tailer.py', (),
      ('await asyncio.to_thread(\n                event_journal_reader.read_events',
       'cursor = await asyncio.to_thread(event_journal_reader.cursor',
      ),
      ('events, _, _ = event_journal_reader.read_events(', 'cursor = event_journal_reader.cursor('),
     ),
    )),
    ('hot_path_warning_logs_are_off_loop', (
     ('grep', 'main.py', (),
      ('_LOG_WRITE_EXECUTOR = ThreadPoolExecutor(', 'def _warning_off_loop(', 'def _frontend_log_off_loop('), (),
     ),
     ('grep', 'main.py', (('async def _event_loop_lag_monitor()', 'asyncio.create_task('),),
      ('_warning_off_loop("event loop lag %.3fs", lag)',), ('logger.warning("event loop lag %.3fs", lag)',),
     ),
     ('grep', 'main.py', (('class _WebSocketOutbox:', '@app.websocket("/ws/chat")'),), ('_warning_off_loop(',),
      ('logger.warning(\n                "slow WebSocket send type=%s elapsed_ms=%.1f"',),
     ),
     ('grep', 'main.py', (('async def frontend_log(', '@app.get("/api/mobile/bundle/manifest")'),),
      ('_frontend_log_off_loop(log_level, line)',), ('frontend_logger.log(log_level, line)',),
     ),
    )),
    ('websocket_json_serializes_off_loop', (
     ('grep', 'main.py', (('class _WebSocketOutbox:', '@app.websocket("/ws/chat")'),),
      ('perf.LaggedQueue(', 'asyncio.create_task(self._writer())', 'self._queue.put_nowait(queued_item)',
       'await self._websocket.send_text(text)', 'await self._on_close()',
       'serialized_task = getattr(event_dict, "_bc_serialized_json_task", None)',
       'text = await asyncio.shield(serialized_task)', 'text = await dumps_ws_json(event_dict)',
       'ws.send_json.serialize_off_loop', 'ws.phase.serializer_submit_start', 'ws.phase.serializer_start_done',
       'ws.phase.serializer_done_writer_dequeue', 'ws.phase.writer_dequeue_await_start',
       'ws.phase.serializer_done_await_resume', 'ws.phase.serializer_resume_wire_start', 'ws.phase.wire_start_resume',
       'ws.phase.lag_overlap',
      ),
      ('timeout=self._send_timeout_s', 'ws.send_json.lock_wait'),
     ),
     ('grep', 'main.py', (('async def ws_callback(event_dict):', '# Per-connection token'),),
      ('return await snapshot_transport.send_event(event_dict)',),
      ('await websocket.send_text(text)', 'await websocket.send_json(event_dict)'),
     ),
     ('grep', 'ws_serialization.py', (),
      ('ThreadPoolExecutor(', 'thread_name_prefix="ws-json"', 'async def dumps_ws_json(',
       'def shutdown_ws_json_executor()',
      ),
      (),
     ),
     ('grep', 'orchestrator.py', (), ('SerializedGlobalEvent',), ()),
     ('grep', 'orchestrator.py', (('def _schedule_prepared_global(', 'async def _schedule_prepared_global_async('),),
      ('_bc_serialized_json_task', 'dumps_ws_json(event)'), (),
     ),
     ('grep', 'ws_snapshot_transport.py', (), ('await asyncio.shield(serialized_task)',), ()),
    )),
    ('resolved_event_reader_keeps_unfiltered_reads_paged', (
     ('grep', 'event_journal.py',
      (('class EventJournalReader', None), ('    def read_events(', '    def read_orphan_events(')),
      ('if msg_id_filter:', 'limit=999_999', 'limit=page_limit + 1'), (),
     ),
     ('grep', 'event_journal.py',
      (('class EventJournalReader', None), ('    def read_events(', '    def read_orphan_events('), ('else:', None)),
      (), ('limit=999_999',),
     ),
    )),
    ('ws_gap_fill_paginates_until_target_seq', (
     ('grep', 'jsonl_tailer.py', (('    async def _fill_gap(', '    async def _send('),),
      ('while self.next_seq <= until_seq:', 'has_more', 'if not has_more:', 'break'), (),
     ),
    )),
    ('jsonl_dispatch_reads_session_lite_off_loop', (
     ('grep', 'jsonl_tailer.py', (), ('await asyncio.to_thread(session_manager.get_lite, self.app_session_id)',),
      ('sess = session_manager.get_lite(self.app_session_id)',),
     ),
    )),
    ('gemini_polling_tailer_reads_file_off_loop', (
     ('grep', 'jsonl_tailer.py', (('class GeminiJsonlTailer', 'class OwnedClaudeJsonlTailer'),),
      ('new_lines = await asyncio.to_thread(self._read_new_lines)',), ('new_lines = self._read_new_lines()',),
     ),
    )),
    ('codex_rollout_tailer_reads_file_off_loop', (
     ('grep', 'codex_native.py',
      (('class CodexRolloutTailer', 'async def _dispatch('),
       ('async def _drain_available_locked(', 'def _read_available_lines('),
      ),
      ('lines = await asyncio.to_thread(', 'self._read_available_lines'), ('self.path.open', '.readline()'),
     ),
     ('grep', 'codex_native.py',
      (('class CodexRolloutTailer', 'async def _dispatch('), ('async def _drain_available_locked(', None),
       ('def _read_available_lines(', None),
      ),
      ('with self.path.open("rb") as f:', 'raw = f.readline()'), (),
     ),
    )),
    ('event_ingester_file_ref_context_uses_summary_projection', (
     ('grep', 'event_ingester.py', (('def _ref_ctx_for_root(', 'class EventIngester:'),),
      ('session_store.summary_fields_many([root_id], ("cwd", "node_id"))',),
      ('session_manager.get_lite(', 'session_manager.get('),
     ),
     ('grep', 'event_ingester.py', (('def _root_dir(', 'def _events_path('),),
      ('session_store.session_file_path(root_id)',), ('bc_home()', 'ba_home()'),
     ),
    )),
    ('ui_selection_uses_cached_path_and_snapshots_written_data', (
     ('grep', 'ui_selection.py', (), ('_PATH = ba_home() / "app-state" / "ui-selection.json"',), ()),
     ('grep', 'ui_selection.py', (('def _path():', 'def _load()'),), (), ('ba_home()',)),
     ('grep', 'ui_selection.py', (('def set_selected_project(', 'def _remembered_sessions_from('),),
      ('return _snapshot(data)',), ('return get_all()',),
     ),
     ('grep', 'ui_selection.py', (('def set_remembered_session(', 'def _snapshot('),), ('return _snapshot(data)',),
      ('return get_all()',),
     ),
    )),
    ('ui_selection_routes_use_hot_path_executor', (
     ('grep', 'bff_app_routes.py', (('async def get_ui_selection(', '@router.patch("/api/ui-selection")'),),
      ('await asyncio.to_thread(ui_selection.get_all)',), (),
     ),
     ('grep', 'bff_app_routes.py', (('async def patch_ui_selection(', None),),
      ('await asyncio.to_thread(_patch_sync)',), (),
     ),
    )),
    ('user_prefs_uses_cached_path_for_hot_reads', (
     ('grep', 'user_prefs.py', (), ('_PREFS_PATH = bc_home() / "user_prefs.json"',), ()),
     ('grep', 'user_prefs.py', (('def _prefs_path():', 'def _load()'),), (), ('bc_home()', 'ba_home()')),
    )),
    ('auto_restart_pref_read_is_off_loop', (
     ('grep', 'auto_restart_on_idle.py', (('async def _tick(', 'busy = await asyncio.to_thread(self._is_busy)'),),
      ('await asyncio.to_thread(self._is_enabled)',), ('if not self._is_enabled():',),
     ),
    )),
    ('session_opened_avoids_full_session_copy', (
     ('grep', 'main.py', (('async def mark_session_opened(', '@app.'),),
      ('return_session=False', 'await _run_hot_path(\n        "session.opened.set_last_opened_at"'),
      ('asyncio.to_thread(',),
     ),
     ('grep', 'session_manager.py', (('def set_last_opened_at(', 'def set_archived('),),
      ('return_session: bool = True', '{"id": sid, "last_opened_at": at}'), (),
     ),
    )),
    ('jsonl_fallback_followers_poll_files_off_loop', (
     ('grep', 'jsonl_tailer.py', (('class _FileTailFollower:', 'class _AppendOnlyByteFollower:'),),
      ('_FILE_POLL_EXECUTOR',), ('size = self._path.stat().st_size',),
     ),
     ('grep', 'jsonl_tailer.py', (('class _AppendOnlyByteFollower:', 'class ClaudeJsonlTailer'),),
      ('_FILE_POLL_EXECUTOR',), ('st = self._path.stat()',),
     ),
     ('grep', 'jsonl_tailer.py',
      (('class _FileTailFollower:', 'class _AppendOnlyByteFollower:'), (None, 'def _read_from_sync')), (),
      ('with open(self._path, "rb") as f:',),
     ),
     ('grep', 'jsonl_tailer.py',
      (('class _AppendOnlyByteFollower:', 'class ClaudeJsonlTailer'), (None, 'def _read_from_sync')), (),
      ('with open(self._path, "rb") as f:',),
     ),
    )),
    ('live_provider_stream_mutation_skips_cold_event_hydration', (
     ('grep', 'turn_manager.py', (),
      ('_STREAM_EVENT_APPLY_EXECUTOR = ThreadPoolExecutor(', 'thread_name_prefix="stream-event-apply"'), (),
     ),
     ('grep', 'turn_manager.py',
      (('    def _apply_provider_stream_event_sync(', '    async def _publish_provider_stream_event('),),
      ('session_manager.message_batch(', 'hydrate_events=False'), (),
     ),
     ('grep', 'turn_manager.py',
      (('async def save_ws_callback(', '            if event_dict.get("type") in _BRIDGE_EVENT_TYPES:'),),
      ('run_in_executor(\n                        _STREAM_EVENT_APPLY_EXECUTOR',),
      ('session_manager.message_batch(', 'with session_manager.batch(persist_to):'),
     ),
    )),
    ('provider_context_runtime_discovery_runs_off_loop', (
     ('grep', 'turn_manager.py',
      (('        runtime_capability_contexts = await _to_turn_dispatch_thread(', '        transient_attempt = 0'),),
      ('runtime_capability_contexts = await _to_turn_dispatch_thread(', 'runtime_skill_contexts,',
       'extension_instruction_contexts = await _to_turn_dispatch_thread(', 'extension_user_instruction_contexts,',
      ),
      (),
     ),
     ('grep', 'turn_manager.py',
      (('        async def _refresh_provider_context()', '        async def _start_selector_change_continuation('),),
      ('runtime_capability_contexts = await _to_turn_dispatch_thread(', 'runtime_skill_contexts,',
       'extension_instruction_contexts = await _to_turn_dispatch_thread(', 'extension_user_instruction_contexts,',
      ),
      (),
     ),
     ('grep', 'turn_manager.py',
      (('        async def _refresh_provider_context()', None),
       ('        async def _start_selector_change_continuation(', None),
       ('        while True:', '            if cancel_event.is_set():'),
      ),
      ('await _refresh_provider_context()',), (),
     ),
    )),
    ('continuation_start_boundary_runs_off_loop', (
     ('grep', 'turn_manager.py',
      (('        loop = asyncio.get_running_loop()', '        async def _clear_continuation_active()'),),
      ('_session_rec = await _to_turn_dispatch_thread(\n            session_manager.get,',
       'provider = await _to_turn_dispatch_thread(\n            self._c.provider_for_run,',
       '_session_rec_chain = (_session_rec or {}).get("continuation_chain") or []',
      ),
      ('session_manager.get(primary_session_id or app_session_id)',),
     ),
     ('grep', 'turn_manager.py',
      (('        async def _clear_continuation_active()', '        def _should_preempt_context_continuation_sync()'),),
      ('await _to_turn_dispatch_thread(\n                session_manager.set_msg_continuation_active',), (),
     ),
     ('grep', 'turn_manager.py',
      (('        def _start_continuation_sync(', '        async def _start_context_continuation('),),
      ('start_continuation_for(', 'session_manager.set_msg_continuation_active('), (),
     ),
     ('grep', 'turn_manager.py',
      (('        def _start_continuation_sync(', None),
       ('        async def _start_context_continuation(',
        '        def _should_preempt_selector_change_continuation_sync()',
       ),
      ),
      ('continuation = await _to_turn_dispatch_thread(\n                _start_continuation_sync,',),
      ('start_continuation_for(', 'session_manager.set_msg_continuation_active('),
     ),
     ('grep', 'turn_manager.py', (('        async def _start_selector_change_continuation(', '        while True:'),),
      ('continuation = await _to_turn_dispatch_thread(\n                _start_continuation_sync,',),
      ('start_continuation_for(', 'session_manager.set_msg_continuation_active('),
     ),
     ('grep', 'turn_manager.py',
      (('        async def _refresh_provider_context()', '        async def _start_selector_change_continuation('),),
      ('_session_rec = await _to_turn_dispatch_thread(\n                session_manager.get,',
       'provider = await _to_turn_dispatch_thread(\n                self._c.provider_for_session,',
      ),
      (),
     ),
     ('grep', 'turn_manager.py',
      (('        async def _context_strategy_is_continuation()', '        async def _refresh_provider_context()'),),
      ('await _to_turn_dispatch_thread(user_prefs.get_context_strategy)',), (),
     ),
     ('grep', 'turn_manager.py',
      (('        async def _start_selector_change_continuation(', None),
       ('            # ── Context-window overflow', '            if (not success'),
      ),
      ('if await _context_strategy_is_continuation():',), ('user_prefs.get_context_strategy()',),
     ),
    )),
    ('provisioning_run_lifecycle_runs_off_loop', (
     ('grep', 'provisioning/manager.py', (('async def run(', 'def _lifecycle_lock('),),
      ('await _ensure_ready_lifecycle(',),
      ('with _acquired_lifecycle_lock(spec, cfg):', 'base_session_id = ensure_session(spec, cfg)',
       'caller_session_id = ensure_caller(spec, cfg)',
      ),
     ),
     ('grep', 'provisioning/manager.py', (('async def _ensure_ready_lifecycle(', '@asynccontextmanager'),),
      ('async with _async_acquired_lifecycle_lock(spec, cfg):', 'await _ensure_ready_base_locked(spec, cfg, ctx)',
       'await asyncio.to_thread(ensure_caller, spec, cfg)',
      ),
      (),
     ),
    )),
    ('requirements_internal_routes_use_dedicated_executor', (
     ('grep', 'main.py', (),
      ('run_requirements_processor_query(\n            "requirements.processed.processor",',
       'run_requirements_query(\n        "requirements.processed.finalize",',
       'requirement_context.recover_processed_requirements_from_delegation',
       'processed = recovered or requirement_context.processor_failure_result(exc)',
       'executor=REQUIREMENTS_PROCESSOR_EXECUTOR', 'run_requirements_query(\n        "requirements.search",',
       'executor=REQUIREMENTS_SEARCH_EXECUTOR',
      ),
      ('_REQUIREMENTS_QUERY_EXECUTOR', '_run_requirements_query',
       'asyncio.to_thread(\n        requirement_context.get_processed_requirements,',
       'asyncio.to_thread(\n        requirement_context.search_requirements,',
      ),
     ),
    )),
    ('worker_panel_mutations_skip_cold_event_hydration', (
     ('grep', 'session_manager.py', (('    def _run(', '    @perf.timed_fn("session.persist_root")'),),
      ('hydrate_events: bool = True', 'self._cached(sid, hydrate_events=hydrate_events)'), (),
     ),
     ('grep', 'session_manager.py', (('    def snapshot_workers(', '\n    def '),), ('hydrate_events=False',), ()),
     ('grep', 'session_manager.py', (('    def upsert_worker_panel(', '\n    def '),), ('hydrate_events=False',), ()),
     ('grep', 'session_manager.py', (('    def update_worker_panel(', '\n    def '),), ('hydrate_events=False',), ()),
     ('grep', 'session_manager.py', (('    def apply_worker_panel_event(', '\n    def '),), ('hydrate_events=False',),
      (),
     ),
    )),
    ('native_event_mutations_skip_cold_event_hydration', (
     ('grep', 'session_manager.py', (('    def append_native_event(', '\n    def '),), ('hydrate_events=False',), ()),
     ('grep', 'session_manager.py', (('    def replace_native_event(', '\n    def '),), ('hydrate_events=False',), ()),
    )),
    ('subagent_watcher_scans_files_off_loop', (
     ('grep', 'jsonl_tailer.py', (),
      ('_SUBAGENT_SCAN_EXECUTOR = ThreadPoolExecutor(', 'thread_name_prefix="subagent-scan"',
       '_SUBAGENT_SCAN_MAX_PENDING_FUTURES = 2', 'def _subagent_scan_semaphore() -> asyncio.Semaphore:',
       '_SUB_DIR_IDLE_POLL_INTERVAL', '_SUB_DIR_IDLE_BACKOFF', '_SUB_DIR_PENDING_FAST_SECONDS',
       'self._subagent_scan_wakeup: Optional[asyncio.Event] = None', 'self._subagent_pending_fast_until = 0.0',
       'def _should_scan_subagents(self) -> bool:',
      ),
      (),
     ),
     ('grep', 'jsonl_tailer.py', (('    def _decode_line(', '    def _advance_cursor('),),
      ('pending_before = self._subagent_pending_count()', 'self._mark_subagent_pending_fast()'), (),
     ),
     ('grep', 'jsonl_tailer.py', (('async def _watch_subagents(', 'def _scan_subagent_files('),),
      ('if self._should_scan_subagents():', 'async with _subagent_scan_semaphore():', 'await loop.run_in_executor(',
       '_SUBAGENT_SCAN_EXECUTOR', 'perf.timed("tailer.subagent_scan")',
       'poll_interval = self._next_subagent_poll_interval(',
       'await asyncio.wait_for(wakeup.wait(), timeout=poll_interval)', 'self._has_fresh_subagent_pending()',
       'self._mark_subagent_pending_fast()',
      ),
      ('await asyncio.to_thread(\n                    self._scan_subagent_files', '.exists()', '.glob(', '.iterdir(',
       '.read_text(',
      ),
     ),
     ('ordered', 'jsonl_tailer.py', (('async def _watch_subagents(', 'def _scan_subagent_files('),),
      (('if self._should_scan_subagents():',), ('async with _subagent_scan_semaphore():',)),
     ),
     ('grep', 'jsonl_tailer.py', (('    def _apply_subagent_scan(', '    def _spawn_sub_tailer('),),
      (') -> int:', 'return applied'), (),
     ),
    )),
    ('delegation_locked_reuses_worker_session_snapshot', (
     ('grep', 'orchs/manager/_delegation.py', (('async def run_delegation_locked(', '    if machine_completion:'),),
      ('worker_session: dict', 'provider_run_config = worker_session.get("provider_run_config")',
       'capability_contexts = worker_session.get("capability_contexts")', 'worker_session.get("reasoning_effort")',
      ),
      ('worker_session_for_path = session_manager.get(worker_agent_session_id)',
       'session_manager.get(worker_agent_session_id)',
      ),
     ),
    )),
    ('async_provider_resolution_runs_off_loop', (
     ('grep', 'orchs/manager/_delegation.py', (('async def run_delegation(', 'async def run_delegation_locked('),),
      ('await asyncio.to_thread(\n                    coordinator.provider_for_session',
       'coordinator.provider_for_session,\n            worker_session_id',
      ),
      ('coordinator.provider_for_session(worker_session_id)',),
     ),
     ('grep', 'orchs/manager/_delegation.py', (('async def run_delegation_locked(', None),),
      ('coordinator.provider_for_run,\n        worker_agent_session_id',),
      ('coordinator.provider_for_run(worker_agent_session_id, provider_id)',),
     ),
     ('grep', 'main.py',
      (('@app.post("/api/internal/headless-generate")', '@app.post("/api/internal/headless-run")'),),
      ('provider = await asyncio.to_thread(coordinator.provider_for_session, session_id)',), (),
     ),
    )),
    ('delegation_state_store_calls_run_off_loop', (
     ('grep', 'orchs/manager/_delegation.py', (('async def run_delegation(', 'async def run_delegation_locked('),),
      ('caller_session = await asyncio.to_thread(session_manager.get',
       'worker_session = await asyncio.to_thread(session_manager.get',
       'worker_record_result = await asyncio.to_thread(\n        _find_worker_record',
      ),
      ('session_manager.get(worker_session_id)', 'worker_store.get_worker(candidate_cwd, worker_session_id)',
       'worker_store.remove_worker(candidate_cwd, worker_session_id)',
      ),
     ),
     ('grep', 'orchs/manager/_delegation.py', (('async def run_delegation_locked(', None),),
      ('await asyncio.to_thread(\n                session_fork_store.get_fork_record',
       'await asyncio.to_thread(session_manager.get, fork_agent_session_id)',
       'await asyncio.to_thread(session_manager.delete, fork_agent_session_id)',
       'fork_bc = await asyncio.to_thread(\n                session_manager.create_delegate_fork',
       'manager_session = await asyncio.to_thread(session_manager.get, app_session_id)',
      ),
      ('session_fork_store.get_fork_record(cwd, app_session_id', 'session_manager.get(fork_agent_session_id)',
       'session_manager.create_delegate_fork(',
      ),
     ),
    )),
    ('run_primary_wraps_cli_prompt_off_loop', (
     ('grep', 'orchs/base.py', (('    async def run_primary(', '    def build_assistant_scaffold('),),
      ('await asyncio.to_thread(\n                self.wrap_cli_prompt, cwd=cwd, prompt=prompt, session=session,',),
      ('else self.wrap_cli_prompt(cwd=cwd, prompt=prompt, session=session)',),
     ),
    )),
    ('provider_event_rewrite_uses_file_ref_context_not_lite_copy', (
     ('grep', 'orchs/base.py', (('def prepare_provider_event_for_journal(', '    def _apply_worker_event('),),
      ('session_manager.get_file_ref_context(app_session_id)', 'assume_exists_for_node(node_id)'),
      ('session_manager.get_lite(app_session_id)',),
     ),
    )),
    ('publish_event_sync_resolves_cwd_without_full_session_copy', (
     ('grep', 'event_journal.py', (('def publish_event_sync(', 'class EventJournalWriter:'),),
      ('session_manager.get_file_ref_context(context_id or session_id)',),
      ('session_manager.get_lite(', 'session_manager.get('),
     ),
    )),
    ('jsonl_dispatch_ingests_orphans_off_loop', (
     ('grep', 'jsonl_tailer.py', (),
      ('accepted, _ = await asyncio.to_thread(\n                    session_manager.run_if_owner',
       'lambda: strategy.ingest_orphan(',
      ),
      (),
     ),
    )),
    ('wire_tailer_subscribe_resolves_root_off_loop', (
     ('grep', 'orchestrator.py', (('async def _subscribe_to_wire_tailer(', '    def _publish_native_demand('),),
      ('root_id = await asyncio.to_thread(\n            session_manager._root_id_for', 'root_id=root_id'),
      ('root_id = session_manager._root_id_for(app_session_id)',),
     ),
    )),
    ('native_demand_publish_does_not_leak_coroutine_without_loop', (
     ('grep', 'orchestrator.py', (('    def _publish_native_demand(', '    def _unsubscribe_from_wire_tailer('),),
      ('loop = asyncio.get_running_loop()', 'except RuntimeError:\n            return',
       'loop.create_task(\n            bus.publish(',
      ),
      ('asyncio.create_task(\n                bus.publish(',),
     ),
    )),
    ('wire_tailer_unsubscribe_uses_cached_subscriber_root', (
     ('grep', 'orchestrator.py', (('    def _unsubscribe_from_wire_tailer(', '    def _maybe_stop_wire_tailer('),),
      ('root_ids.add(sub.root_id)',), ('session_manager._root_id_for',),
     ),
     ('grep', 'orchestrator.py', (('    def _maybe_stop_wire_tailer(', '    async def _await_tailer_stop('),),
      ('def _maybe_stop_wire_tailer(self, root_id: str, app_session_id: str)',), ('session_manager._root_id_for',),
     ),
    )),
    ('root_session_write_does_not_resolve_root_id', (
     ('grep', 'session_store.py', (('def write_session_full(', 'def delete_session('),),
      ('path = _root_file_path(root["id"])',), ('_session_path(root["id"])', '_resolve_root_id(root'),
     ),
    )),
    ('session_first_prompt_search_uses_summary_index', (
     ('grep', 'session_store.py', (('def _build_summary_for_root(', 'def set_requirement_tags_projection('),),
      ('"first_prompt": _first_user_prompt(root)',), (),
     ),
     ('grep', 'session_store.py', (('def _metadata_search_scores(', 'def grep_session_scores('),),
      ('rows = _metadata_search_rows()', 'for sid, title, first_prompt in rows:',
       'score = first_prompt.count(query_lower)',
      ),
      ('json.loads(path.read_text', '_migrate_session('),
     ),
    )),
    ('session_content_search_aggregates_in_sqlite', (
     ('grep', 'session_search_index.py', (('def search(', 'def has_indexed_rows('),),
      ('_candidate_scores(conn, q, limit)', 'event = _inflight_event_for_limit(q, limit)'), ('lower().count',),
     ),
     ('grep', 'session_search_index.py', (), ('def _inflight_event_for_limit(',), ()),
     ('grep', 'session_search_index.py', (('def _candidate_scores(', 'def _match_literal('),),
      ('COUNT(*) AS score', 'GROUP BY session_id ORDER BY score DESC LIMIT ?'), ('SELECT session_id, text',),
     ),
    )),
    ('bounded_session_content_search_stops_sqlite_scan', (
     ('grep', 'session_search_index.py', (('def search(', 'def has_indexed_rows('),),
      ('args=(cache_key, q, limit, max_wait_seconds, event)',), (),
     ),
     ('grep', 'session_search_index.py', (('def _run_search_cache_fill(', 'def has_indexed_rows('),),
      ('deadline = (', '_candidate_scores(conn, query, limit, deadline=deadline)'), (),
     ),
     ('grep', 'session_search_index.py', (('def _candidate_scores(', 'def _match_literal('),),
      ('conn.set_progress_handler(', 'time.monotonic() >= deadline', 'conn.set_progress_handler(None, 0)',
       'interrupted',
      ),
      (),
     ),
    )),
    ('session_content_search_uses_readonly_connection_without_writer_lock', (
     ('grep', 'session_search_index.py', (),
      ('_readonly_conn_local = threading.local()', 'def _readonly_connection()', '_WRITER_CACHE_KIB = 200_000',
       '_READONLY_CACHE_KIB = 8_192',
      ),
      (),
     ),
     ('grep', 'session_search_index.py', (('def search(', 'def has_indexed_rows('),),
      ('conn = _readonly_connection()',), ('conn.close()', 'with _lock:', '_connect()'),
     ),
     ('grep', 'session_search_index.py', (('def _connect_readonly(', 'def _configure_connection('),),
      ('_configure_connection(conn, readonly=True)',), (),
     ),
     ('grep', 'session_search_index.py', (('def _configure_connection(', 'def _event_text('),),
      ('cache_kib = _READONLY_CACHE_KIB if readonly else _WRITER_CACHE_KIB',
       'conn.execute(f"PRAGMA cache_size=-{cache_kib}")', 'PRAGMA temp_store=MEMORY', 'PRAGMA mmap_size=268435456',
      ),
      (),
     ),
    )),
    ('session_search_delete_is_queued_projection_work', (
     ('grep', 'session_search_index.py', (), ('_writer_conn', 'def _writer_connection()'), ()),
     ('grep', 'session_search_index.py', (('def delete_session(', 'def search('),),
      ('_queue.put((session_id, None))',), ('with _lock:',),
     ),
     ('grep', 'session_search_index.py', (('def _apply_rows_to_conn(', 'def _drain_pending('),),
      ('conn = _writer_connection()', 'DELETE FROM session_event_fts'), ('conn.close()', 'conn = _connect()'),
     ),
    )),
    ('event_journal_rejects_late_writes_after_close', (
     ('grep', 'event_journal.py', (),
      ('self._closed = False', 'self._closed = True',
       'raise EventJournalWriteError("event journal writer is closed")',
      ),
      (),
     ),
    )),
    ('publish_event_default_path_skips_temp_ack_subscribers', (
     ('grep', 'event_journal.py',
      (('async def publish_event(', 'def publish_event_sync('),
       ('if bus_instance is bus:', 'loop = asyncio.get_running_loop()'),
      ),
      ('event_journal_writer.submit_event_async(Event(',), ('bus_instance.subscribe(', 'event_journal_ack_'),
     ),
    )),
    ('broadcast_session_journal_write_runs_off_loop', (
     ('grep', 'orchestrator.py', (('async def broadcast_session(', 'async def broadcast_global('),),
      ('await publish_event(',), ('await asyncio.to_thread(', '_broadcast_session_sync', 'publish_event_sync('),
     ),
    )),
    ('provider_complete_watcher_filesystem_poll_runs_off_loop', (
     ('grep', 'provider.py', (),
      ('def _new_provider_poll_executor()', 'def reopen_provider_tasks() -> None:',
       'thread_name_prefix="provider-poll"', 'async def path_exists_off_loop(path: Path) -> bool:',
       'run_in_executor(_PROVIDER_POLL_EXECUTOR, path.exists)', 'async def shutdown_provider_tasks() -> None:',
       '_PROVIDER_TASKS_ACCEPTING = False', 'await asyncio.gather(*tasks, return_exceptions=True)',
      ),
      (),
     ),
     ('grep', 'provider_claude.py', (('async def _watch_complete(', 'async def _watch_process_exit('),),
      ('await path_exists_off_loop(complete_path)',),
      ('await asyncio.to_thread(complete_path.exists)', 'complete_path.exists()'),
     ),
     ('any_in', 'provider_claude.py', (('async def _bootstrap_run(', 'if runner_state is None:'),),
      ('await path_exists_off_loop(state_path)', 'await path_exists_off_loop(runner_state_path)'),
     ),
     ('grep', 'provider_claude.py', (('async def _bootstrap_run(', 'if runner_state is None:'),),
      ('await path_exists_off_loop(complete_path)',),
      ('state_path.exists()', 'runner_state_path.exists()', 'complete_path.exists()'),
     ),
     ('grep', 'provider_codex.py', (('async def _watch_complete(', 'async def _ensure_child_tailer('),),
      ('await path_exists_off_loop(complete_path)',),
      ('await asyncio.to_thread(complete_path.exists)', 'complete_path.exists()'),
     ),
     ('any_in', 'provider_codex.py', (('async def _bootstrap_run(', 'if runner_state is None:'),),
      ('await path_exists_off_loop(state_path)', 'await path_exists_off_loop(runner_state_path)'),
     ),
     ('grep', 'provider_codex.py', (('async def _bootstrap_run(', 'if runner_state is None:'),),
      ('await path_exists_off_loop(complete_path)',),
      ('state_path.exists()', 'runner_state_path.exists()', 'complete_path.exists()'),
     ),
     ('grep', 'provider_gemini.py',
      (('async def _watch_complete(',
        '# ------------------------------------------------------------------\n    # _emit_complete_from_file',
       ),
      ),
      ('await path_exists_off_loop(complete_path)',),
      ('await asyncio.to_thread(complete_path.exists)', 'complete_path.exists()'),
     ),
     ('any_in', 'provider_gemini.py', (('async def _bootstrap_run(', 'if runner_state is None:'),),
      ('await path_exists_off_loop(state_path)', 'await path_exists_off_loop(runner_state_path)'),
     ),
     ('grep', 'provider_gemini.py', (('async def _bootstrap_run(', 'if runner_state is None:'),),
      ('await path_exists_off_loop(complete_path)',),
      ('state_path.exists()', 'runner_state_path.exists()', 'complete_path.exists()'),
     ),
     ('grep', 'provider_openai.py', (('async def _watch_complete(', 'async def _emit_complete_from_file('),),
      ('await path_exists_off_loop(complete_path)',),
      ('await asyncio.to_thread(complete_path.exists)', 'complete_path.exists()'),
     ),
     ('any_in', 'provider_openai.py', (('async def _bootstrap_run(', 'if runner_state is None:'),),
      ('await path_exists_off_loop(state_path)', 'await path_exists_off_loop(runner_state_path)'),
     ),
     ('grep', 'provider_openai.py', (('async def _bootstrap_run(', 'if runner_state is None:'),),
      ('await path_exists_off_loop(complete_path)',),
      ('state_path.exists()', 'runner_state_path.exists()', 'complete_path.exists()'),
     ),
    )),
    ('codex_cursor_state_write_is_coalesced_off_loop', (
     ('grep', 'provider_codex.py', (('        def _on_cursor(', '        rs.tailer = CodexRolloutTailer('),),
      ('_rs.processed_byte_offset = n', 'pending.cursor = n'),
      ('self._write_backend_state(_rs)', 'self._schedule_backend_state_flush(_rs)'),
     ),
     ('grep', 'provider_codex.py', (('        def _on_child_cursor(', '        tailer = CodexRolloutTailer('),),
      ('self._schedule_backend_state_flush(_rs)',), ('self._write_backend_state(_rs)',),
     ),
     ('grep', 'provider_codex.py', (('    async def _flush_backend_state_async(', '    def attach_recovered_run('),),
      ('await asyncio.to_thread(self._write_backend_state, rs)',), (),
     ),
    )),
    ('internal_workers_list_runs_projection_off_loop', (
     ('grep', 'main.py', (('async def internal_list_workers_for_cwd(', '@app.'),),
      ('return await asyncio.to_thread(_internal_list_workers_for_cwd_sync, cwd)',),
      ('compute_jsonl_path(', 'count_jsonl_lines(', 'session_manager.get_lite('),
     ),
     ('grep', 'team_orchestration_read.py', (),
      ('session_store.summary_fields_many(worker_sids, _SESSION_FIELDS)', 'with perf.timed(f"{_METRIC}.session")',
       'pair_records: list[dict[str, Any]] = []',
      ),
      ('extension.team_orchestration.workers.fallback_fields', 'session_manager.get_fields_many(',
       'session_manager.get_fields(\n            bc_sid', 'session_manager.get_lite(',
      ),
     ),
     ('ordered', 'team_orchestration_read.py', (), (('pair_records.append(rec)',), ('compute_jsonl_path(',))),
    )),
    ('message_delta_replay_skips_full_snapshot_rebuild', (
     ('grep', 'session_manager.py',
      (('def get_messages_since(', 'def _get_cached_snapshot('),
       ('if since_seq > 0:', 'snapshot = self._get_cached_snapshot('),
      ),
      ('_get_cached_messages_window(',), ('_compute_messages_window(', '_get_cached_snapshot('),
     ),
     ('grep', 'session_manager.py', (('def _get_cached_messages_window(', 'def _tree_stub_cache_key('),),
      ('_compute_messages_window(', '_copy_jsonish(cached)'), (),
     ),
     ('grep', 'session_manager.py', (('def _compute_messages_window(', 'def get_ref('),),
      ('summary_ids = {', 'summaries = self._native_event_summaries(\n            rid, node_sid, summary_ids,'), (),
     ),
    )),
    ('message_summary_reader_filters_requested_message_ids', (
     ('grep', 'event_ingester.py',
      (('def message_event_summaries(', '@staticmethod\n    def _public_message_summary'),),
      ('msg_ids: Optional[set[str]] = None', 'if not sid_filter and msg_ids is None:',
       'self._summary_matches_filter(k, v, sid_filter=sid_filter, msg_ids=msg_ids)',
      ),
      (),
     ),
     ('grep', 'event_journal.py', (('def message_event_summaries(', 'def current_seq('),),
      ('msg_ids: Optional[set[str]] = None', 'msg_ids=msg_ids'), (),
     ),
    )),
    ('event_summary_sidecar_load_populates_memory_cache', (
     ('grep', 'event_ingester.py', (),
      ('_EVENT_SUMMARIES_VERSION = 5', 'def _valid_seq_offsets(', 'isinstance(item, bool)'), (),
     ),
     ('grep', 'event_ingester.py', (('def _summaries_state(', 'def _seq_byte_range('),),
      ('sid_filter: Optional[str] = None', 'msg_ids: Optional[set[str]] = None', 'if loaded is not None:',
       'self._summaries_cache[root_id] = (\n                        file_size, summaries, resolutions,',
      ),
      (),
     ),
     ('grep', 'event_ingester.py',
      (('def _summaries_state(', 'def _seq_byte_range('), ('if loaded is not None:', 'else:')), (),
      ('_rebuild_seq_offsets_locked',),
     ),
    )),
    ('connected_session_fallback_sorts_only_requested_page', (
     ('grep', 'main.py', (), ('def _filter_sort_page_for_list(',), ()),
     ('grep', 'main.py',
      (('async def get_sessions(', '@app.post("/api/sessions/search-content")'),
       ('if can_page_remote_local_order:', 'elif _can_preserve_summary_order'),
      ),
      ('_filter_sort_page_for_list',), ('_filter_sort_sessions_for_list',),
     ),
    )),
    ('message_cache_hydration_has_substep_perf_metrics', (
     ('grep', 'event_journal.py', (),
      ('DEFAULT_MESSAGE_CACHE_SIZE = 128', 'message_cache_size: int = DEFAULT_MESSAGE_CACHE_SIZE'), (),
     ),
     ('grep', 'event_journal.py', (('def _ensure_message_cache(', 'def read_message_frontend_events('),),
      ('event_journal.message_cache.summaries', 'summary: Optional[dict] = None', 'msg_ids={message_id}',
       'event_journal.message_cache.summary_provided', 'event_ingester.ownership_resolutions_range(',
       'event_journal.message_cache.resolutions', 'event_journal.message_cache.read_full',
       'event_journal.message_cache.read_grow',
      ),
      ('event_ingester.ownership_resolutions(session_id)',),
     ),
    )),
    ('session_snapshot_hydration_reuses_existing_message_summary', (
     ('grep', 'session_manager.py', (('def _compute_messages_snapshot(', 'def _compute_messages_window('),),
      ('summary = summaries.get(msg_id, {})', 'message_id=msg_id,\n                        summary=summary,'), (),
     ),
     ('grep', 'session_manager.py', (('def _compute_messages_window(', 'def get_ref('),),
      ('message_id=msg_id,\n                    summary=summary,',), (),
     ),
    )),
    ('written_journal_projection_avoids_full_event_list_copy', (
     ('grep', 'session_manager.py', (('    def apply_written_journal_event(', '    def _root_id_for('),),
      ('event_uuid = _event_uuid_safe', 'compact_message_delta_payload(msg)', '"delta": delta'),
      ('before = copy.deepcopy(strategy._events_list(msg))', '"msg": _copy_jsonish(msg)'),
     ),
    )),
    ('slow_path_instrumentation_separates_queue_wait_from_work', (
     ('grep', 'session_manager.py', (),
      ('"session.tail_persist.root_lock_wait"', '"session.tail_persist.root_lock_held"'), (),
     ),
     ('grep', 'turn_manager.py', (),
      ('provider.start_run.recovery_gate', 'provider.start_run.flush_root_persist',
       'provider.start_run.provider_call', 'with perf.timed("provider.start_run.recovery_gate")',
       'with perf.timed("provider.start_run.provider_call")',
      ),
      (),
     ),
     ('grep', 'turn_manager.py',
      (('with perf.timed("provider.start_run.recovery_gate")', '                target_message_id ='),),
      ('wait_for_session_recovery_ready(\n                        app_session_id,',), ('wait_for_recovery_ready()',),
     ),
     ('grep', 'orchs/manager/_delegation.py', (),
      ('"delegate.provider_start_run.recovery_gate"', '"delegate.provider_start_run.provider_call"'), (),
     ),
     ('count_ge', 'orchs/_subprocess_agent.py', (), 'await asyncio.to_thread(', 2),
     ('grep', 'orchs/_subprocess_agent.py', (),
      ('"subprocess_agent.init.start_run.provider_call"', '"subprocess_agent.run.start_run.provider_call"'),
      ('\n                provider.start_run(',),
     ),
     ('grep', 'node_rpc_handlers.py', (), ('"node_rpc.provider_start_run.provider_call"',), ()),
     ('grep', 'main.py', (), ('perf.LaggedQueue(', '_perf_name="ws.outbox"'), ()),
    )),
    ('duplicate_journal_acks_do_not_enter_row_projections', (
     ('grep', 'event_journal.py', (), ('"appended": written.seq > 0',), ()),
     ('count_ge', 'event_bus_subscribers.py', (), 'int(payload.get("seq") or 0) <= 0', 2),
     ('grep', 'event_bus_subscribers.py',
      (('def bind_session_content_projection()', 'def bind_requirement_tags_projection()'),), (),
      ('name="session_search_projection"',),
     ),
    )),
    ('completion_file_read_runs_on_bounded_provider_executor', (
     ('grep', 'provider.py', (),
      ('async def run_provider_poll_off_loop', 'run_in_executor(_PROVIDER_POLL_EXECUTOR, fn, *args)'), (),
     ),
     ('grep', 'provider_claude.py',
      (('    async def _emit_complete_from_file(', '    async def _emit_early_failure('),),
      ('await run_provider_poll_off_loop(read_best_complete, rs.run_dir)',),
      ('best = read_best_complete(rs.run_dir)',),
     ),
    )),
    ('perf_counts_are_not_reported_as_latency', (
     ('grep', 'perf.py', (), ('def record_count(', 'count_total=', 'count_max='), ()),
     ('grep', 'session_manager.py', (), ('perf.record_count("session.hydrate_todos.rows", len(all_rows))',),
      ('perf.record("session.hydrate_todos.rows"',),
     ),
    )),
    ('ingester_and_ownership_hydration_expose_lock_phases', (
     ('grep', 'event_ingester.py', (),
      ('"ingest.live.root_lock_wait"', '"ingest.live.root_lock_held"', '"ingest.batch.root_lock_wait"',
       '"ingest.batch.root_lock_held"', '"ingest.read_events.root_lock_wait"', '"ingest.read_events.root_lock_held"',
       'self._enqueue_search_projection(root_id, search_entry)',
      ),
      (),
     ),
     ('grep', 'event_journal.py', (),
      ('ejw.ownership_hydrate.snapshot', 'ejw.ownership_hydrate.read', 'ejw.ownership_hydrate.replay',
       'ejw.ownership_hydrate.resolve_pending',
      ),
      (),
     ),
    )),
    ('node_link_runtime_readiness_uses_ttl_cache', (
     ('grep', 'node_link.py', (), ('_MACHINE_NODES_READY_CACHE_TTL_S',), ()),
     ('grep', 'node_link.py', (('def _machine_nodes_not_ready_reason(', 'def set_registration_listener('),),
      ('time.monotonic()', '_machine_nodes_ready_cache', 'runtime_not_ready_message('), (),
     ),
    )),
    ('projection_preserving_summary_reuses_existing_projection', (
     ('grep', 'session_store.py', (('def _build_summary_for_root_preserving_projections(', 'def _tag_filter_ids('),),
      ('projection_snapshot=(', 'organization_projection=(', 'existing.get("requirement_tags")',
       'existing.get("markers")', 'existing.get("session_tags")',
      ),
      ('_requirement_tags_for_session(', '_markers_for_session(', 'enrich_session_summary(summary)'),
     ),
     ('grep', 'session_store.py', (('def _upsert_summary(', 'def _seen_cursor_path('),),
      ('if preserve_projection_fields:', '_build_summary_for_root_preserving_projections(root, existing)',
       'for field in _SUMMARY_PROJECTION_FIELDS:',
      ),
      (),
     ),
    )),
    ('connected_session_list_pages_virtual_candidates', (
     ('grep', 'main.py',
      (('    if connected:', '@app.post("/api/sessions/search-content")'), ('if may_include_virtual:', 'try:')),
      ('if can_page_remote_local_order:', 'virtual_session_store.list_recent', 'max(offset + limit, 1)',
       'virtual_session_store.list_all',
      ),
      (),
     ),
    )),
    ('connected_session_list_skips_full_sort_without_remote_merge', (
     ('grep', 'main.py', (('async def get_sessions(', '@app.post("/api/sessions/search-content")'),),
      ('appended_remote_sessions = False',
       'can_page_remote_local_order\n        and not appended_virtual_sessions\n        and not appended_remote_sessions\n        and local_total is not None',
      ),
      (),
     ),
     ('ordered', 'main.py', (('async def get_sessions(', '@app.post("/api/sessions/search-content")'),),
      (('can_page_remote_local_order\n        and not appended_virtual_sessions\n        and not appended_remote_sessions\n        and local_total is not None',
       ),
       ('with perf.timed("sessions.list.filter_sort")',),
      ),
     ),
    )),
    ('delegation_status_writes_run_off_loop', (
     ('grep', 'operation_status_store.py', (),
      ('async def write_status_async(', 'await asyncio.to_thread(self.write_status'), (),
     ),
     ('grep', 'delegation_status_store.py', (), ('write_status_async = _store.write_status_async',), ()),
     ('grep', 'orchs/manager/_delegation.py', (('async def run_delegation(', None),),
      ('await delegation_status_store.write_status_async(',), ('delegation_status_store.write_status(',),
     ),
    )),
    ('team_ask_status_writes_run_off_loop', (
     ('grep', 'operation_status_store.py', (),
      ('async def write_status_async(', 'await asyncio.to_thread(self.write_status'), (),
     ),
     ('grep', 'ask_status_store.py', (), ('write_status_async = _store.write_status_async',), ()),
     ('grep', 'orchestrator.py', (('async def ask_team_message(', '    def _team_message_turn_response('),),
      ('sender, target = await asyncio.to_thread(\n            team_messaging.validate_message_route',
       'metadata = await asyncio.to_thread(\n            team_messaging.build_message_metadata',
       'queue_item = await asyncio.to_thread(\n                team_messaging.queue_payload',
       'await asyncio.to_thread(\n                session_manager.add_queued_prompt',
       'cli_prompt = await asyncio.to_thread(\n                team_messaging.format_team_message_prompt',
       'await ask_status_store.write_status_async(',
      ),
      ('session_manager.add_queued_prompt(', 'cli_prompt = team_messaging.format_team_message_prompt(',
       'ask_status_store.write_status(',
      ),
     ),
    )),
    ('team_message_context_uses_lite_session_read', (
     ('grep', 'team_messaging.py', (('def _target_team_context(', 'def format_team_message_prompt('),),
      ('session_manager.get_lite(target_session_id)',), ('session_manager.get(target_session_id)',),
     ),
    )),
    ('team_message_validation_uses_lite_session_read', (
     ('grep', 'team_messaging.py', (('def validate_message_route(', 'def build_message_metadata('),),
      ('session_manager.get_lite(sender_session_id)', 'session_manager.get_lite(target_session_id)'),
      ('session_manager.exists(', 'session_manager.get('),
     ),
    )),
    ('known_worker_projection_uses_field_reads', (
     ('grep', 'stores/worker_store.py', (('def list_worker_projection(', '@perf.timed_fn("store.worker.upsert")'),),
      ('_sm.get_fields_many(',),
      ('_sm.get_fields(agent_session_id', '_sm.get(agent_session_id)', '_sm.get_lite(agent_session_id)'),
     ),
    )),
    ('session_exists_uses_index_without_cold_root_load', (
     ('grep', 'session_manager.py', (('    def exists(self, sid: str) -> bool:', '    def get_field('),),
      ('session_store._resolve_root_id(sid)', 'session_store._loaded_root_id_for(sid)',
       'session_store.session_file_fingerprint(sid)',
      ),
      ('self._load_root(',),
     ),
     ('ordered', 'session_manager.py', (('    def exists(self, sid: str) -> bool:', '    def get_field('),),
      (('session_store._loaded_root_id_for(sid)',), ('session_store.session_file_fingerprint(sid)',)),
     ),
     ('ordered', 'session_manager.py', (('    def exists(self, sid: str) -> bool:', '    def get_field('),),
      (('session_store.session_file_fingerprint(sid)',), ('session_store._resolve_root_id(sid)',)),
     ),
     ('count_eq', 'session_manager.py', (('    def exists(self, sid: str) -> bool:', '    def get_field('),),
      'session_store._find_in_tree(root, sid)', 1,
     ),
    )),
    ('root_id_resolution_caches_successful_store_lookup', (
     ('grep', 'session_manager.py', (('    def _root_id_for(', '    def _lock_for_root('),),
      ('rid = self._node_root_id.get(sid)', 'session_store._loaded_root_id_for(sid)',
       'session_store.session_file_fingerprint(sid)', 'self._node_root_missing_until.get(sid, 0.0) > now',
       'rid = session_store._resolve_root_id(sid)', 'if rid is not None:\n            self._node_root_id[sid] = rid',
       'self._node_root_missing_until[sid] = (',
      ),
      (),
     ),
     ('ordered', 'session_manager.py', (('    def _root_id_for(', '    def _lock_for_root('),),
      (('session_store._loaded_root_id_for(sid)',), ('session_store.session_file_fingerprint(sid)',)),
     ),
     ('ordered', 'session_manager.py', (('    def _root_id_for(', '    def _lock_for_root('),),
      (('session_store.session_file_fingerprint(sid)',), ('rid = session_store._resolve_root_id(sid)',)),
     ),
     ('grep', 'session_manager.py', (), ('_NEGATIVE_NODE_ROOT_TTL_SECONDS = 5.0',), ()),
     ('grep', 'session_manager.py', (('    def _index_root(', '    def _ensure_root_loaded('),),
      ('self._node_root_missing_until.pop(rid, None)', 'self._node_root_missing_until.pop(fork["id"], None)'), (),
     ),
    )),
    ('unknown_root_resolution_uses_owner_projection_without_rescan', (
     ('grep', 'session_store.py', (('def _resolve_root_id(', 'def _session_path('),),
      ('_wait_root_change_owner_ready()', 'generation = owner.observation_generation',
       '_wait_root_change_observation(generation)',
      ),
      ('_dir_fingerprint_cached()', '_refresh_index('),
     ),
    )),
    ('fork_index_refresh_sidecar_write_is_backgrounded', (
     ('grep', 'session_store.py', (), ('_index_sidecar_write_queue', 'def _schedule_index_sidecar_write('), ()),
     ('grep', 'session_store.py', (('def _refresh_index(', 'def _ensure_index('),),
      ('_schedule_index_sidecar_write(fp, fork_index, root_forks, root_signatures)',),
      ('_write_index_sidecar_best_effort(fp, fork_index, root_forks, root_signatures)',),
     ),
     ('grep', 'session_store.py', (('def _ensure_index(', 'def _resolve_root_id('),),
      ('_schedule_index_sidecar_write(fp, fork_index, root_forks, root_signatures)',), (),
     ),
    )),
    ('fork_index_refresh_updates_changed_roots_incrementally', (
     ('grep', 'session_store.py', (),
      ('_INDEX_INCREMENTAL_REFRESH_MAX_CHANGED = 32', 'def _refresh_index_incremental('), (),
     ),
     ('grep', 'session_store.py', (('def _refresh_index_incremental(', 'def _load_index_sidecar('),),
      ('changed_roots = {', 'deleted_roots = set(old_signatures) - set(current_signatures)',
       'if len(touched_roots) > _INDEX_INCREMENTAL_REFRESH_MAX_CHANGED:',
       '_fork_index_entry_from_summary_or_root(current_paths[root_id])',
      ),
      (),
     ),
     ('ordered', 'session_store.py', (('def _refresh_index(', 'def _ensure_index('),),
      (('incremental = _refresh_index_incremental(live_fp)',),
       ('with perf.timed("store.session.index.refresh.build")',),
      ),
     ),
    )),
    ('session_detail_reuses_migrated_root_cache', (
     ('grep', 'session_store.py', (), ('_migrated_root_cache', 'def _cached_migrated_root('), ()),
     ('grep', 'session_store.py', (('def _cached_migrated_root(', 'def read_node_kind_record('),),
      ('cache_key = (root_id, file_signature)', 'return _copy_jsonish(cached)'), (),
     ),
     ('grep', 'session_store.py', (('def get_root_tree(', 'def _strip_volatile_from_tree('),),
      ('_cached_migrated_root(root_id, file_signature, root)',), (),
     ),
    )),
    ('extension_plain_load_is_read_only', (
     ('grep', 'extension_store.py', (('def _load()', 'def _save('),), ('_read_store_unlocked()',),
      ('_load_with_changes()',),
     ),
    )),
    ('recovery_dispatch_skips_reconciled_runs_before_owner_read', (
     ('ordered', 'provider.py', (('def recover_all_in_flight(', None),),
      (('indexed_marker = reconciled_index.get(child.name)',), ('marker_path = child / "reconciled.marker"',)),
     ),
     ('ordered', 'provider.py', (('def recover_all_in_flight(', None),),
      (('marker_path = child / "reconciled.marker"',), ('bs_path = child / "backend_state.json"',)),
     ),
     ('grep', 'provider.py',
      (('def recover_all_in_flight(', None), (None, 'indexed_marker = reconciled_index.get(child.name)')),
      ('load_reconciled_marker_index_for(',), (),
     ),
     ('grep', 'provider.py',
      (('def recover_all_in_flight(', None),
       ('marker_path = child / "reconciled.marker"', 'bs_path = child / "backend_state.json"'),
      ),
      ('marker_data_matches_current(',), ('marker_matches_current(',),
     ),
    )),
    ('filtered_provider_recovery_does_not_rescan_all_runs', (
     ('grep', 'runs_dir.py', (),
      ('def iter_run_dirs(run_id_filter: Optional[set[str]] = None)', 'for run_id in run_id_filter:'), (),
     ),
     ('grep', 'provider_claude.py',
      (('    def recover_in_flight(', '    # ------------------------------------------------------------------'),),
      ('iter_run_dirs(run_id_filter)',), ('child.name not in run_id_filter',),
     ),
     ('grep', 'provider_codex.py',
      (('    def recover_in_flight(', '    # ------------------------------------------------------------------'),),
      ('iter_run_dirs(run_id_filter)',), ('child.name not in run_id_filter',),
     ),
     ('grep', 'provider_gemini.py',
      (('    def recover_in_flight(', '    # ------------------------------------------------------------------'),),
      ('iter_run_dirs(run_id_filter)',), ('child.name not in run_id_filter',),
     ),
     ('grep', 'provider_openai.py',
      (('    def recover_in_flight(', '    # ------------------------------------------------------------------'),),
      ('iter_run_dirs(run_id_filter)',), ('child.name not in run_id_filter',),
     ),
    )),
    ('filtered_remote_recovery_does_not_rescan_all_runs', (
     ('grep', 'run_recovery.py',
      (('def _pending_remote_runs_for_node(', 'async def integrate_remote_runs_for_node('),),
      ('iter_run_dirs(run_id_filter)', 'children = sorted(children)'), ('child.name not in run_id_filter',),
     ),
    )),
    ('provider_prune_uses_shared_scandir_helper', (
     ('grep', 'runs_dir.py', (),
      ('def prune_old_completed_runs(max_age_days: int = 7) -> int', 'with os.scandir(root) as entries:'), (),
     ),
     ('grep', 'provider_claude.py',
      (('    def prune_old_runs(', '    # ------------------------------------------------------------------'),),
      ('prune_old_completed_runs(max_age_days)',), ('_runs_root().iterdir()',),
     ),
     ('grep', 'provider_codex.py',
      (('    def prune_old_runs(', '    # ------------------------------------------------------------------'),),
      ('prune_old_completed_runs(max_age_days)',), ('_runs_root().iterdir()',),
     ),
     ('grep', 'provider_gemini.py',
      (('    def prune_old_runs(', '    # ------------------------------------------------------------------'),),
      ('prune_old_completed_runs(max_age_days)',), ('_runs_root().iterdir()',),
     ),
     ('grep', 'provider_openai.py',
      (('    def prune_old_runs(', '    # ------------------------------------------------------------------'),),
      ('prune_old_completed_runs(max_age_days)',), ('_runs_root().iterdir()',),
     ),
    )),
    ('session_fork_index_refresh_is_root_scoped', (
     ('grep', 'session_store.py', (('def _index_tree(', 'def _index_set('),),
      ('_root_forks.get(rid', '_root_index_signatures.get(rid) == file_signature'),
      ('_fork_index.items()', '_reconcile_loaded_store'),
     ),
     ('ordered', 'session_store.py', (('def _index_tree(', 'def _index_set('),),
      (('_root_index_signatures.get(rid)',), ('for fork in _walk_forks(root)',)),
     ),
     ('grep', 'session_store.py', (('def get_root_tree(', 'def _strip_volatile_from_tree('),),
      ('file_signature = _session_file_signature(path)', '_index_tree(root, file_signature=file_signature)',
       'if session_id != root_id:',
      ),
      (),
     ),
     ('ordered', 'session_store.py', (('def get_root_tree(', 'def _strip_volatile_from_tree('),),
      (('if session_id != root_id:',), ('_index_tree(root, file_signature=file_signature)',)),
     ),
    )),
    ('session_organization_reads_are_cached', (
     ('grep', 'session_organization_store.py', (),
      ('_cache_signature', '_cache_data', '_path_cache', 'def _load_shared()'), (),
     ),
     ('grep', 'session_organization_store.py', (('def _path():', 'def _now()'),),
      ('ba_home()', 'if _path_cache is not None', 'return _path_cache[1]'), (),
     ),
     ('grep', 'session_organization_store.py', (('def _load()', 'def _save('),), ('return copy.deepcopy(data)',), ()),
     ('grep', 'session_organization_store.py', (('def _load_shared()', 'def _load()'),),
      ('_cache_signature == signature', 'return _cache_data'), (),
     ),
     ('grep', 'session_organization_store.py', (('def enrich_session_summaries(', 'def create_folder('),),
      ('data = _load_shared()',), ('_assignment(',),
     ),
    )),
    ('jsonl_cursor_advance_is_synchronous_and_non_blocking', (
     ('grep', 'jsonl_tailer.py', (), (), ('_CURSOR_EXECUTOR',)),
     ('grep', 'jsonl_tailer.py', (('async def _notify_cursor(', 'def _advance_cursor('),),
      ('self.on_cursor_advance(self.processed_offset)',), ('run_in_executor',),
     ),
     ('grep', 'provider_claude.py', (('def _on_tailer_progress(', '\n\n'),),
      ('cursor_ledger_worker.note(', 'lambda: self._write_backend_state('), (),
     ),
     ('grep', 'provider_gemini.py', (('def _on_cursor(', '\n\n'),),
      ('_rs.processed_line = n', 'pending.cursor = n'),
      ('self._write_backend_state(', 'run_in_executor'),
     ),
     ('grep', 'provider_openai.py', (('def _on_cursor(', '\n\n'),),
      ('_rs.processed_line = n', 'pending.cursor = n'),
      ('self._write_backend_state(', 'run_in_executor'),
     ),
    )),
    ('event_ingester_indexes_search_outside_root_lock', (
     ('grep', 'event_ingester.py', (), (), ('session_search_index',)),
    )),
    ('local_extension_reconcile_skips_current_snapshot', (
     ('grep', 'extension_store.py',
      (('def _ensure_local_extensions(', 'def _install_required_marketplace_from_ofekdev('),),
      ('source.get("package_sha256") == package_sha', 'manifest == record.get("manifest")', 'install_path.is_dir()'),
      (),
     ),
     ('ordered', 'extension_store.py',
      (('def _ensure_local_extensions(', 'def _install_required_marketplace_from_ofekdev('),),
      (('install_path.is_dir()', 'continue'),
       ('install_path.is_dir()', 'continue', '_refresh_local_extension_snapshot('),
      ),
     ),
    )),
    ('frontend_entrypoints_do_not_run_smoke_subprocesses', (
     ('grep', 'extension_store.py', (('def _record_runtime_ready(', 'def _record_has_required_runtime_paths('),),
      ('_record_smoke_test_current(record)',),
      ('_record_smoke_test_passes(record)', '_run_extension_smoke_test(', '_run_python_module_smoke('),
     ),
     ('grep', 'extension_store.py', (('def frontend_entrypoints(', 'def resolve_frontend_asset('),),
      ('_record_runtime_ready(record)',), ('_run_extension_smoke_test(',),
     ),
    )),
    ('extension_list_uses_projection_cache', (
     ('grep', 'extension_store.py', (('def list_extensions(', 'def _active_records('),),
      ('_projection_cache_get("list_extensions"', '_projection_cache_put(\n        "list_extensions",',
       'return list_extensions(include_hidden=include_hidden), False',
      ),
      (),
     ),
    )),
    ('extension_projection_routes_cache_json_bytes', (
     ('grep', 'extension_api.py', (),
      ('_projection_response_cache', 'def _projection_response_cache_get(', 'def _projection_response_cache_put(',
       'def _cached_json_projection_response(', 'async def _cached_json_projection_response_threaded(', 'json.dumps(',
       'Response(content=content, media_type="application/json")',
      ),
      (),
     ),
     ('grep', 'extension_api.py', (('async def get_frontend_entrypoints(', '@router.get("/ui-hooks")'),),
      ('await _cached_json_projection_response_threaded(', 'extension_store.frontend_entrypoints_cache_key,',
       'extension_store.frontend_entrypoints()',
      ),
      (),
     ),
     ('grep', 'extension_api.py',
      (('async def get_ui_hooks(', '@router.get("/{extension_id}/frontend/{asset_path:path}")'),),
      ('await _cached_json_projection_response_threaded(', 'extension_store.ui_hooks_cache_key,',
       'extension_store.ui_hooks()',
      ),
      (),
     ),
    )),
    ('startup_reenqueue_reads_sessions_off_loop', (
     ('grep', 'main.py', (), ('await asyncio.to_thread(\n                    session_manager.get_lite',), ()),
    )),
    ('queue_projection_scans_user_messages_once', (
     ('grep', 'session_queue_projection.py', (), ('def _user_message_projection(',),
      ('def _user_message_keys(', 'def _user_messages('),
     ),
     ('grep', 'session_queue_projection.py', (('def project_session(', 'def _walk_nodes('),),
      ('users = _user_message_projection(session.get("messages") or [])', '**users'), (),
     ),
    )),
    ('queue_projection_skips_unchanged_disk_write', (
     ('grep', 'session_queue_projection.py', (('def _apply_mutation(', 'def upsert_record('),),
      ('current_record == owned', '_regresses_queue_revision(current_record, owned)'), (),
     ),
     ('grep', 'session_queue_projection.py', (('def _compact_batch(', 'def _compact_rebuild('),),
      ('elif existing[0] != payload:', 'else:\n                continue'), (),
     ),
    )),
    ('queue_projection_overlay_reads_records_in_bulk', (
     ('grep', 'session_queue_projection.py', (), ('def get_many(',), ()),
     ('grep', 'session_store.py', (('def _overlay_queue_projection(', '@perf.timed_fn("store.session.write_full")'),),
      ('session_queue_projection.get_many(sids)',), ('session_queue_projection.get(sid)',),
     ),
    )),
    ('queue_projection_shutdown_always_closes_executor', (
     ('grep', 'main.py', (('        begin_queue_projection_shutdown()', '    except Exception:'),),
      ('try:', 'finally:', 'await asyncio.to_thread(shutdown_queue_projection_executor)'), (),
     ),
     ('ordered', 'main.py', (('        begin_queue_projection_shutdown()', '    except Exception:'),),
      (('finally:',), ('await asyncio.to_thread(shutdown_queue_projection_executor)',)),
     ),
    )),
    ('startup_does_not_warm_unread_by_hydrating_sessions', (
     ('grep', 'main.py', (), (), ('startup-unread-warm', '_warm_unread_counts')),
    )),
    ('startup_defers_requirement_and_project_match_warmers', (
     ('grep', 'main.py', (('async def on_startup()', 'async def on_shutdown()'),),
      ('name="requirements-processor-prewarm"',),
      ('\n    await requirement_prewarm.run_requirements_prewarm', 'project-match-warm'),
     ),
     ('grep', 'main.py', (), ('_ensure_project_match_warm_task()',), ()),
    )),
    ('startup_extension_package_resolution_stays_off_loop', (
     ('grep', 'main.py', (('async def _on_startup_bg_orchestrator()', 'def _startup_orchestrator_done'),),
      ('await asyncio.to_thread(\n                extension_package_loader.ensure_package_importable,',),
      ('\n            extension_package_loader.ensure_package_importable(',),
     ),
     ('grep', 'requirement_prewarm.py', (('async def warm_processor()', 'return await ensure_warm_base'),),
      ('spec, cfg = await asyncio.to_thread(resolve_processor)',),
      ('\n            spec = requirement_context.get_requirements_processor_spec()',),
     ),
    )),
    ('requirement_unprocessed_fallback_reuses_freshness_projection', (
     ('grep', 'requirement_context.py', (('def _load_unprocessed_prompt_records(', 'def _prompt_fallback_record('),),
      ('freshness.get("_unhandled_prompt_records")',), ('for prompt in load_prompts()',),
     ),
     ('grep', 'requirement_context.py', (), ('"freshness": _public_freshness(freshness)',), ()),
    )),
    ('startup_defers_shortcut_http_prewarm', (
     ('grep', 'main.py', (('async def on_startup()', 'async def on_shutdown()'),),
      ('shortcut_picker.prewarm_http_stack',
       '_fire_and_forget(asyncio.to_thread(shortcut_picker.prewarm_http_stack))',
      ),
      ('await asyncio.to_thread(shortcut_picker.prewarm_http_stack)',),
     ),
    )),
    ('sidebar_organization_enrichment_stays_in_summary_index', (
     ('grep', 'main.py', (('def _local_session_summaries_for_sidebar()', 'def _root_session_file_path('),), (),
      ('enrich_session_summaries(', 'enrich_session_summary(', 'session_store._ensure_summary_index(blocking=True)'),
     ),
     ('grep', 'session_store.py', (('def _build_summary_for_root(', 'def set_requirement_tags_projection('),),
      ('enrich_session_summary(summary)', 'enrich_session_summary_from_projection('), (),
     ),
     ('grep', 'session_organization_store.py', (('def enrich_session_summary(', 'def enrich_session_summaries('),),
      ('_load_shared()',), ('organization_for_session(', '_load()'),
     ),
    )),
    ('session_organization_facets_are_version_cached', (
     ('grep', 'main.py', (), ('_session_org_facets_cache',), ()),
     ('grep', 'main.py',
      (('def _session_organization_snapshot_with_facets(', '@app.get("/api/session-organization")'),),
      ('session_organization_store.version_token()', 'session_store.summary_version()',
       '_session_org_facets_cache.get(cache_key)', '_local_session_summaries_for_sidebar()',
      ),
      (),
     ),
    )),
    ('session_organization_query_builds_tag_sets_only_for_tag_filter', (
     ('ordered', 'session_organization_store.py', (('def query_sessions(', None),),
      (('if tag_set:',), ('session_tags = {',)),
     ),
    )),
    ('sidebar_decoration_uses_bulk_cached_state', (
     ('grep', 'main.py', (), ('def _sidebar_state_snapshot()', '_sidebar_state_snapshot_cache'), ()),
     ('grep', 'main.py', (('def _sidebar_state_snapshot()', 'def _decorate_local_sidebar_sessions('),),
      ('version = _sessions_list_transient_state_version()', 'cached is not None and cached[0] == version',
       'pending_input_by_sid = user_input_store.pending_counts_by_session()',
      ),
      (),
     ),
     ('grep', 'main.py', (('def _sidebar_session_payload(', 'def _sidebar_state_snapshot('),),
      ('if key != "first_prompt"',), ('payload.pop("first_prompt", None)',),
     ),
     ('grep', 'main.py', (('def _decorate_local_sidebar_sessions(', 'def _local_sessions_for_sidebar('),),
      ('_sidebar_state_snapshot()',), ('is_running_cached(', 'monitoring_state_cached('),
     ),
     ('grep', 'main.py', (('def _build_local_sessions_page_for_list(', 'async def _sidebar_search_scores('),),
      ('state_snapshot = _sidebar_state_snapshot() if status_sort else None',
       '_decorate_local_sidebar_sessions(out[offset:end], state_snapshot)',
      ),
      (),
     ),
     ('grep', 'turn_manager.py', (), ('def cached_state_snapshot(',), ()),
    )),
    ('session_discovery_reads_mode_without_deepcopy', (
     ('grep', 'turn_manager.py',
      (('if event.type == "session_discovered":', 'if event.type in ("complete", "error"):'),),
      ('session_manager.get_field(', '"orchestration_mode"'), ('session_manager.get(',),
     ),
    )),
    ('project_aggregates_use_bulk_cached_state', (
     ('grep', 'main.py', (('def _project_aggregates(', 'def _invalidate_project_aggregates('),),
      ('monitoring_projection_snapshot()', 'unread_counts_snapshot()'), ('is_running_cached(', 'peek_unread_count('),
     ),
    )),
    ('sidebar_file_paths_use_cached_sessions_dir', (
     ('grep', 'main.py', (), ('def _root_sessions_dir_path(',), ()),
     ('grep', 'main.py', (('def _decorate_local_sidebar_sessions(', 'def _local_sessions_for_sidebar('),),
      ('sessions_dir = _root_sessions_dir_path()', '"file_path": f"{sessions_dir}/{sid}.json"'),
      ('ba_home()', '_root_session_file_path(sid)'),
     ),
    )),
    ('session_list_uses_sorted_summary_cache', (
     ('grep', 'session_store.py', (),
      ('_summary_sorted_cache_version', '_summary_sorted_id_cache', '_summary_sorted_id_caches',
       '_summary_order_version', '_replace_summary_projection_field', 'def ordered_session_summary_ids(',
       'def _summary_order_changed(', '"last_user_prompt_at"',
      ),
      ('_summary_projected_cache_version', '_summary_projected_cache'),
     ),
     ('grep', 'session_store.py', (('def list_sessions()', 'def iter_all_sessions()'),),
      ('_summary_sorted_cache_version != _summary_order_version', '_summary_sorted_id_cache = [',
       'sorted(\n                    _summary_index.values()', '_summary_index[sid]',
      ),
      ('_requirement_tags_snapshot()', '_markers_snapshot()'),
     ),
    )),
    ('session_list_pages_last_user_prompt_order_before_full_sort', (
     ('grep', 'main.py', (('def _local_session_page_for_sidebar_preserving_order(', 'def _root_session_file_path('),),
      ('session_manager.ordered_summary_ids(sort_by, folder_view)', 'sessions.list.local.ordered_filter'),
      ('_filter_sort_sessions_for_list(',),
     ),
     ('grep', 'main.py', (('def _can_page_local_summary_order(', 'def _build_local_sessions_page_for_list('),),
      ('sort_by in {"updated_at", "last_user_prompt_at", "last_opened_at"}',), (),
     ),
     ('grep', 'main.py', (('def _build_local_sessions_page_for_list(', '@app.get("/api/sessions")'),),
      ('sort_by == "last_user_prompt_at"', 'sessions.list.local_order_page', 'sessions.list.virtual_count',
       'if default_virtual_page:', 'limit=max(offset + limit, 1)',
      ),
      (),
     ),
     ('ordered', 'main.py', (('def _build_local_sessions_page_for_list(', '@app.get("/api/sessions")'),),
      (('sessions.list.local_order_page',), ('sessions.list.local"):',)),
     ),
     ('grep', 'main.py', (('async def get_sessions(', '@app.post("/api/sessions/search-content")'),),
      ('sessions.list.remote.local_order_candidates', 'can_page_remote_local_order'), (),
     ),
     ('ordered', 'main.py', (('async def get_sessions(', '@app.post("/api/sessions/search-content")'),),
      (('sessions.list.remote.local_order_candidates',), ('sessions.list.local"):',)),
     ),
    )),
    ('visible_order_cache_uses_dual_generation_singleflight_projection', (
     ('grep', 'session_store.py', (('def sidebar_session_summary_page(', 'def get_session_summaries_by_ids('),),
      ('_summary_order_version,\n            _summary_visibility_version,',
       'visible_ids = _sidebar_page_projections.get(key)',
       'store.session.sidebar_page.projection_hit', 'store.session.sidebar_page.projection_miss',
       'while len(_sidebar_page_projections) > _SIDEBAR_PAGE_PROJECTIONS_MAX:',
      ),
      (),
     ),
     ('grep', 'main.py', (('def _local_session_page_for_sidebar_preserving_order(', 'def _root_session_file_path('),),
      ('session_store.sidebar_session_summary_page(',), (),
     ),
    )),
    ('session_list_skips_impossible_virtual_filters', (
     ('grep', 'main.py', (('def _session_filters_may_include_virtual(', 'def _build_local_sessions_page_for_list('),),
      ('if file_edit_mode is True:', 'if folder_ids or tag_ids:', 'if modes and "virtual" not in modes:',
       'if sources and not ({"extension", "system"} & sources):',
      ),
      (),
     ),
     ('grep', 'main.py', (('def _build_local_sessions_page_for_list(', '@app.get("/api/sessions")'),),
      ('_session_filters_may_include_virtual(', 'virtual_session_store.list_recent(', 'max(offset + limit, 1)',
       'perf.record("sessions.list.virtual.skipped", 1.0)',
      ),
      (),
     ),
     ('grep', 'main.py', (('async def get_sessions(', '@app.post("/api/sessions/search-content")'),),
      ('_session_filters_may_include_virtual(', 'virtual_session_store.list_all',
       'perf.record("sessions.list.virtual.skipped", 1.0)',
      ),
      (),
     ),
    )),
    ('session_list_preserves_summary_order_when_no_virtual_rows', (
     ('grep', 'main.py', (('def _can_preserve_summary_order(', 'def _session_filters_may_include_virtual('),),
      ('appended_virtual_sessions: bool', 'and not appended_virtual_sessions'), ('virtual_sessions: list[dict]',),
     ),
     ('grep', 'main.py', (('def _build_local_sessions_page_for_list(', '@app.get("/api/sessions")'),),
      ('appended_virtual_sessions = False', 'virtual_sidebar_sessions = [',
       '_can_page_default_updated_at_with_virtual(', '_merge_updated_at_page(', 'if virtual_sidebar_sessions:',
       'appended_virtual_sessions = True', 'appended_virtual_sessions=appended_virtual_sessions',
       '_filter_page_for_list_preserving_order(', '_decorate_local_sidebar_sessions(page_source, state_snapshot)',
      ),
      (),
     ),
     ('grep', 'main.py', (('def _filter_page_for_list_preserving_order(', 'def _can_preserve_summary_order('),),
      ('page.append(session)', 'return page, total'), (),
     ),
     ('grep', 'main.py', (('async def get_sessions(', '@app.post("/api/sessions/search-content")'),),
      ('appended_virtual_sessions = False', 'virtual_sidebar_sessions = [', 'if virtual_sidebar_sessions:',
       'appended_virtual_sessions = True', '_filter_sessions_for_list_preserving_order',
      ),
      (),
     ),
    )),
    ('session_tag_filter_uses_summary_projection', (
     ('grep', 'session_store.py', (),
      ('"tag_filter_ids": _tag_filter_ids(', 'summary["tag_filter_ids"] = _tag_filter_ids(',
       '"tag_filter_ids": tag_filter_ids',
      ),
      (),
     ),
     ('grep', 'main.py', (('def _session_matches_list_filters(', 'def _session_filtered_sort_key('),),
      ('filter_ids = session.get("tag_filter_ids")', '_session_tag_filter_ids(session)'),
      ('manual_tags = {', 'requirement_tags = {'),
     ),
    )),
    ('session_timestamp_sort_value_is_cached', (
     ('grep', 'session_store.py', (),
      ('from functools import lru_cache', '@lru_cache(maxsize=4096)\ndef _timestamp_sort_value_str'), (),
     ),
     ('grep', 'session_store.py', (('def timestamp_sort_value(', 'def _newer_timestamp('),),
      ('return _timestamp_sort_value_str(value)',), (),
     ),
    )),
    ('shortcut_picker_wait_budget_is_small', (
     ('grep', 'shortcut_picker.py', (), ('_PICK_WAIT_TIMEOUT_SECS = 0.25',), ()),
     ('grep', 'shortcut_picker.py', (('async def pick_shortcuts(', None),),
      ('asyncio.wait_for(', 'await asyncio.to_thread(\n            _shortcut_picker_inputs,',
       'fallback_shortcuts = list(all_shortcuts)', 'return await asyncio.shield(_cached_pick(key, _pick_uncached))',
       'return all_shortcuts',
      ),
      ('user_prefs.get_shortcut_responses()', 'config_store.get_default_provider()'),
     ),
     ('grep', 'shortcut_picker.py',
      (('async def pick_shortcuts(', None), ('except asyncio.TimeoutError:', 'except Exception:')),
      ('if fallback_shortcuts is not None:',), (),
     ),
     ('ordered', 'shortcut_picker.py',
      (('async def pick_shortcuts(', None), ('except asyncio.TimeoutError:', 'except Exception:')),
      (('if fallback_shortcuts is not None:',), ('await asyncio.to_thread(user_prefs.get_shortcut_responses)',)),
     ),
    )),
    ('stubbed_tree_build_does_not_search_tree_per_node', (
     ('grep', 'session_manager.py', (('def _build_stubbed_tree(', 'def _compute_messages_snapshot('),),
      ('node_sid, rid, node_src',), ('session_store._find_in_tree(root, node_sid)',),
     ),
    )),
    ('tree_stub_cache_key_reads_render_seq_once', (
     ('grep', 'session_manager.py', (('def _tree_stub_cache_key(', 'def _build_stubbed_tree('),),
      ('render_seq_by_sid = event_ingester.render_seq_by_sid(rid)',), ('render_seq_for_sid(',),
     ),
    )),
    ('session_event_meta_uses_combined_ingester_read', (
     ('grep', 'main.py', (('def _session_event_meta_roots_for_page(', 'async def _warm_session_event_meta_roots('),),
      (), ('_session_event_file_fingerprint(root_id) == (0, 0)',),
     ),
     ('grep', 'main.py', (('def _session_event_meta(', 'def _session_event_meta_cache_fresh('),),
      ('event_ingester.session_event_meta(root_id)',),
      ('event_ingester.max_seq_by_sid(root_id)', 'event_ingester.cursor(root_id)',
       'event_ingester.render_seq_by_sid(root_id)',
      ),
     ),
     ('grep', 'event_ingester.py', (), ('def session_event_meta(self, root_id: str)',), ()),
     ('grep', 'event_ingester.py', (('def _scan_max_seq(', 'def close('),),
      ('summaries: dict[str, dict] = {}', 'self._update_summary_line(',
       'self._summaries_cache[root_id] = (cur_offset, summaries, resolutions)',
      ),
      (),
     ),
    )),
    ('event_summary_scan_reuses_full_scan_cache', (
     ('grep', 'event_ingester.py', (('def _scan_max_seq(', '    @staticmethod\n    def _affects_render_projection'),),
      ('entries: list[dict] = []', 'self._remember_full_scan_cache_locked(root_id, cur_offset, entries)',
       'self._seq_offsets[root_id] = seq_offsets',
      ),
      (),
     ),
     ('grep', 'event_ingester.py', (('def _scan_summaries(', '    def close('),),
      ('cached = self._full_scan_cache.get(root_id)', 'entries = cached[1]', 'self._update_summary_line(',
       'for index, entry in enumerate(entries):',
      ),
      (),
     ),
    )),
    ('message_hydration_reuses_full_scan_cache', (
     ('grep', 'event_ingester.py', (), ('def cached_rows_for_byte_range(',), ()),
     ('grep', 'event_ingester.py', (('def cached_rows_for_byte_range(', 'def root_events_by_sid('),),
      ('cached = self._full_scan_cache.get(root_id)', 'bisect.bisect_left(offsets, byte_start)',
       'line_start >= byte_end', 'rows.append(entry)',
      ),
      (),
     ),
     ('grep', 'event_journal.py', (('def _read_owned_range(', 'def _read_raw_range('),),
      ('event_ingester.cached_rows_for_byte_range(', 'if raw is None:', 'self._read_raw_range('), (),
     ),
    )),
    ('read_events_collects_page_without_filtered_copies', (
     ('grep', 'event_ingester.py', (('    @perf.timed_fn("ingest.read_events")', '    def _extend_full_scan('),),
      ('out: list[dict] = []', 'if len(out) < page_limit:', 'return out, total, has_more'),
      ('filtered = [e for e in filtered',),
     ),
    )),
    ('metadata_session_search_uses_metadata_version_cache', (
     ('grep', 'session_store.py', (),
      ('_metadata_search_cache', '_metadata_text_cache', '_metadata_text_by_id_cache', '_metadata_trigram_index',
       '_METADATA_NGRAM_MAX_SIZE = 3', '_start_metadata_search_index_warm()',
       '_metadata_text_cache: tuple[tuple[str, str, str], ...] = ()', '_summary_metadata_version',
      ),
      (),
     ),
     ('grep', 'session_store.py', (('def _metadata_search_rows(', 'def _metadata_search_scores('),),
      ('str(summary.get("name") or "").lower()', 'str(summary.get("first_prompt") or "").lower()',
       '_metadata_text_cache_version == _summary_metadata_version', 'return _metadata_text_cache', 'rows = tuple(',
      ),
      ('return list(_metadata_text_cache)',),
     ),
     ('grep', 'session_store.py', (('def _metadata_search_row_map(', 'def _metadata_ngrams('),),
      ('_metadata_text_by_id_cache_version == version',
       'row_map = {sid: (title, first_prompt) for sid, title, first_prompt in rows}',
      ),
      (),
     ),
     ('grep', 'session_store.py', (('def _metadata_search_scores(', 'def grep_session_scores('),),
      ('cache_key = (query_lower, metadata_fields, _summary_metadata_version)',
       'cached = _metadata_search_cache.get(cache_key)', 'return dict(cached)',
       'candidate_ids = _metadata_candidate_ids(query_lower, metadata_fields)',
       'row_map = _metadata_search_row_map()', 'for sid in candidate_ids', 'rows = _metadata_search_rows()',
       'for sid, title, first_prompt in rows:', 'title.count(query_lower)', 'first_prompt.count(query_lower)',
       '_metadata_search_cache[cache_key] = dict(scores)',
      ),
      ('if candidate_ids is not None and sid not in candidate_ids:',),
     ),
     ('grep', 'session_store.py', (('def _metadata_candidate_ids(', 'def _metadata_search_scores('),),
      ('grams = _metadata_query_grams(query_lower)', '_start_metadata_search_index_warm()', 'return None'),
      ('_metadata_search_index_for_current_version()', '_metadata_trigrams(query_lower)'),
     ),
    )),
    ('search_summary_lookup_uses_maintained_projection', (
     ('grep', 'session_store.py', (), (), ('_requirement_tags_for_sessions', '_markers_for_sessions')),
     ('grep', 'session_store.py', (('def get_session_summaries_by_ids(', 'def iter_all_sessions()'),),
      ('return [\n            _summary_index[sid]',), (),
     ),
    )),
    ('sessions_response_cache_stores_serialized_bytes', (
     ('grep', 'main.py', (),
      ('tuple[float, bytes, tuple[int, int, int]]', '_SESSIONS_LIST_RESPONSE_TTL_SECONDS = 15.0',
       'def _sessions_list_transient_state_version()', 'session_manager.monitoring_projection_version()',
       'session_manager.unread_counts_version()', 'user_input_store.pending_counts_version_loaded()',
      ),
      ('def _sessions_list_transient_fingerprint(',),
     ),
     ('grep', 'main.py', (('def _sessions_list_cache_get(', '_GIT_STATUS_TTL_SECONDS'),),
      ('return _sessions_list_response(cached[1])', 'json.dumps(',
       'cached[2] != _sessions_list_transient_state_version()',
      ),
      ('copy.deepcopy',),
     ),
    )),
    ('sidebar_payload_reuses_summary_projection_cache', (
     ('grep', 'main.py', (),
      ('_sidebar_payload_cache', '_SIDEBAR_PAYLOAD_CACHE_MAX', '_sidebar_decorated_cache',
       '_SIDEBAR_DECORATED_CACHE_MAX',
      ),
      (),
     ),
     ('grep', 'main.py', (('def _sidebar_session_payload(', 'def _sidebar_state_snapshot('),),
      ('cache_key = id(session)', '_sidebar_payload_cache.get(cache_key)', 'return cached[1]',
       '_sidebar_payload_cache[cache_key] = (sid, payload)',
      ),
      (),
     ),
     ('grep', 'main.py', (('def _decorate_local_sidebar_sessions(', 'def _local_sessions_for_sidebar('),),
      ('decorated_cache_key = (', 'summary_version = session_store.summary_index_version()',
       'sid,\n                summary_version,', 'pending_user_input_count,',
       '_sidebar_decorated_cache.get(decorated_cache_key)',
       '_sidebar_decorated_cache[decorated_cache_key] = decorated',
      ),
      (),
     ),
    )),
    ('search_sessions_response_cache_uses_metadata_version', (
     ('grep', 'main.py', (('def _sessions_list_cache_version(', '_GIT_STATUS_TTL_SECONDS'),),
      ('session_store.search_metadata_version()', 'session_search_index.generation()',
       'session_store.SEARCH_FIELD_CONTENT in search_fields', 'session_store.summary_version()',
       'virtual_session_store.version_token()',
      ),
      (),
     ),
     ('grep', 'main.py', (('async def get_sessions(', '@app.post("/api/sessions/search-content")'),),
      ('_sessions_list_cache_version(search_query, effective_search_fields)',
       'cached_response = _sessions_list_cache_get(cache_key)',
       'effective_search_fields = _split_session_search_fields(search_fields)',
       'tuple(sorted(effective_search_fields))',
      ),
      ('_sessions_list_transient_state_version()', 'cache_response = not ('),
     ),
     ('grep', 'main.py',
      (('async def get_sessions(', '@app.post("/api/sessions/search-content")'), ('cache_key = (', ')')),
      ('search_query',), ('\n        search,\n',),
     ),
     ('grep', 'session_store.py', (), ('def search_metadata_version()', 'return _summary_metadata_version'), ()),
    )),
    ('session_summaries_response_cache_precedes_lookup', (
     ('grep', 'main.py', (('@app.get("/api/sessions/summaries")', '@app.get("/api/sessions/{session_id}/stats")'),),
      ('cached_response = _session_summaries_cache_get(cache_key)',), (),
     ),
     ('ordered', 'main.py',
      (('@app.get("/api/sessions/summaries")', '@app.get("/api/sessions/{session_id}/stats")'),),
      (('cached_response = _session_summaries_cache_get(cache_key)',), ('_local_session_summaries_by_ids',)),
     ),
     ('ordered', 'main.py',
      (('@app.get("/api/sessions/summaries")', '@app.get("/api/sessions/{session_id}/stats")'),),
      (('cached_response = _session_summaries_cache_get(cache_key)',), ('_decorate_local_sidebar_sessions',)),
     ),
     ('grep', 'main.py',
      (('@app.get("/api/sessions/summaries")', '@app.get("/api/sessions/{session_id}/stats")'),
       ('cache_key = (', 'cached_response = _session_summaries_cache_get(cache_key)'),
      ),
      (), ('_sessions_list_transient_state_version()',),
     ),
    )),
    ('session_list_waits_briefly_for_partial_summary_warm', (
     ('grep', 'main.py', (),
      ('_SESSION_LIST_SUMMARY_WARM_WAIT_SECONDS = 0.08', '_SESSION_LIST_SUMMARY_WARM_MIN_PUBLISHED = 50'), (),
     ),
     ('grep', 'main.py',
      (('def _local_session_summaries_for_sidebar()', 'def _local_session_summaries_by_ids_for_sidebar('),),
      ('sessions.list.local.summary_warm_wait', 'min_published=_SESSION_LIST_SUMMARY_WARM_MIN_PUBLISHED'), (),
     ),
     ('grep', 'session_store.py', (('def wait_for_summary_index(', 'def _replace_summary_projection_field('),),
      ('_ensure_summary_index(blocking=False)', 'min_published: int | None = None', 'len(_summary_index) >= target',
       '_summary_build_lock.acquire(timeout=max(0.0, timeout_seconds))',
      ),
      ('_do_build_summary_index_unsafe()',),
     ),
    )),
    ('session_search_projection_enqueue_stays_on_event_loop', (
     ('grep', 'event_bus_subscribers.py',
      (('async def _refresh_session_search_projection(', 'async def _refresh_requirement_tags('),),
      ('_enqueue_session_search_projection(event.root_id, entry)',), ('asyncio.to_thread(',),
     ),
    )),
    ('sidebar_session_search_bounds_content_scoring', (
     ('grep', 'main.py', (), ('_SESSION_LIST_CONTENT_SEARCH_MAX_WAIT_SECONDS',), ()),
     ('grep', 'main.py', (('async def _sidebar_search_scores(', '@app.get("/api/sessions")'),),
      ('if session_store.SEARCH_FIELD_CONTENT in selected_search_fields',), ('metadata_max_wait_seconds',),
     ),
     ('count_eq', 'main.py',
      (('def _build_local_sessions_page_for_list(', '@app.post("/api/sessions/search-content")'),),
      'content_max_wait_seconds = (', 2,
     ),
     ('grep', 'main.py', (('def _build_local_sessions_page_for_list(', '@app.post("/api/sessions/search-content")'),),
      (), ('metadata_max_wait_seconds',),
     ),
     ('grep', 'main.py',
      (('@app.post("/api/sessions/search-content")', '@app.post("/api/session-organization/query")'),), (),
      ('content_max_wait_seconds', 'metadata_max_wait_seconds'),
     ),
     ('grep', 'session_search_index.py', (('def _searchable_event_text(', 'def _content_searchable_text('),),
      ('role = message.get("role") if isinstance(message, dict) else None', 'role = data.get("type")'), (),
     ),
    )),
    ('pending_node_polling_uses_public_projection_cache', (
     ('grep', 'main.py',
      (('async def internal_list_pending_nodes(', '@app.post("/api/internal/machine-nodes/approve")'),),
      ('node_link.public_pending_nodes_cached()', 'await asyncio.to_thread(node_link.public_pending_nodes)'),
      ('pending_node_registrations.list_pending()',),
     ),
     ('grep', 'extension_api.py',
      (('async def _dispatch_machine_nodes_core_backend(', 'async def _dispatch_project_structure_core_backend('),),
      ('node_link.public_pending_nodes_cached()', 'await asyncio.to_thread(node_link.public_pending_nodes)'), (),
     ),
    )),
    ('machine_node_snapshot_reads_are_off_loop', (
     ('grep', 'main.py', (('async def internal_get_nodes(', '@app.get("/api/providers")'),),
      ('await asyncio.to_thread(node_store.snapshot)',), ('node_store.snapshot()',),
     ),
     ('grep', 'main.py',
      (('async def internal_list_pending_nodes(', '@app.post("/api/internal/machine-nodes/approve")'),),
      ('node_link.public_pending_nodes_cached()', 'await asyncio.to_thread(node_link.public_pending_nodes)'), (),
     ),
     ('grep', 'extension_api.py',
      (('async def _dispatch_machine_nodes_core_backend(', 'async def _dispatch_project_structure_core_backend('),),
      ('await asyncio.to_thread(node_store.snapshot)', 'node_link.public_pending_nodes_cached()',
       'await asyncio.to_thread(node_link.public_pending_nodes)', '_local_node_id_or_primary_cached()',
      ),
      ('node_store.snapshot()', 'await asyncio.to_thread(_local_node_id_or_primary'),
     ),
    )),
    ('node_snapshot_caches_static_specs', (
     ('grep', 'node_store.py', (),
      ('_snapshot_static_cache_key', '_snapshot_static_cache', 'def _node_registry_fingerprint()',
       'node_registry_store.version_token()', 'def _snapshot_static_specs()',
      ),
      (),
     ),
     ('grep', 'node_registry_store.py', (),
      ('def version_token()', '_cache_lock = threading.Lock()', '_ensure_cache_locked()'), (),
     ),
     ('grep', 'node_registry_store.py', (('def version_token()', 'def hash_secret('),),
      ('_sync_generation_locked()',), ('_ensure_cache_locked()', '_dir().glob', '.stat()'),
     ),
     ('grep', 'node_registry_store.py', (('def list_all()', 'def remove('),), ('_ensure_cache_locked()',),
      ('_dir().glob',),
     ),
     ('grep', 'node_store.py', (('def snapshot()', 'def connected_worker_node_ids_snapshot()'),),
      ('specs = _snapshot_static_specs()',), ('node_registry_store.list_all()', 'load_topology().all_nodes()'),
     ),
    )),
    ('pending_approval_listing_uses_cached_projection_off_loop', (
     ('grep', 'stores/pending_approvals.py', (),
      ('_pending_cache_lock = threading.Lock()', '_pending_cache:', 'def _invalidate_pending_cache()',
       'def _pending_snapshot()',
      ),
      (),
     ),
     ('grep', 'stores/pending_approvals.py', (('def list_pending(', '@perf.timed_fn("store.approval.transition")'),),
      ('records = _pending_snapshot()',), ('_dir().glob("*.json")', 'path.read_text'),
     ),
     ('grep', 'stores/pending_approvals.py', (('def create(', 'def get('),), ('_invalidate_pending_cache()',), ()),
     ('grep', 'stores/pending_approvals.py', (('def _transition_locked(', 'def approve('),),
      ('_invalidate_pending_cache()',), (),
     ),
     ('grep', 'main.py',
      (('async def internal_list_pending_approvals(', '@app.post("/api/internal/tool-approvals/request")'),),
      ('await asyncio.to_thread(pending_approvals.list_pending, cwd=cwd)',),
      ('pending_approvals.list_pending(cwd=cwd)',),
     ),
    )),
    ('credential_consent_listing_uses_cached_projection_off_loop', (
     ('grep', 'credential_broker/consent_store.py', (),
      ('_pending_cache_lock = threading.Lock()', 'def _pending_snapshot()'), (),
     ),
     ('grep', 'credential_broker/consent_store.py', (('def list_pending(', 'def _expired('),),
      ('records = _pending_snapshot()',), ('_dir().glob', 'path.read_text'),
     ),
     ('grep', 'main.py',
      (('async def internal_list_pending_credentials(', '@app.post("/api/internal/credential-ui/approve")'),),
      ('await asyncio.to_thread(_cs.list_pending, app_session_id=app_session_id)',),
      ('_cs.list_pending(app_session_id=app_session_id)',),
     ),
    )),
    ('project_update_counts_batch_uses_single_store_call', (
     ('grep', 'project_update_store.py', (),
      ('def unseen_counts(project_ids: list[str])', 'def peek_unseen_counts(project_ids: list[str])'), (),
     ),
     ('grep', 'main.py',
      (('async def internal_project_update_counts_batch(', '@app.post("/api/internal/project-updates/unseen")'),),
      ('counts = project_update_store.peek_unseen_counts(project_ids)', 'if counts is None:',
       'await asyncio.to_thread(project_update_store.unseen_counts, project_ids)',
      ),
      ('project_update_store.unseen_count(project_id)',),
     ),
    )),
    ('session_list_does_not_prewarm_snapshots', (
     ('grep', 'main.py', (), (), ('_schedule_session_snapshot_prewarm', 'sessions.snapshot_prewarm')),
     ('grep', 'main.py', (('async def get_sessions(', '@app.post("/api/sessions/search-content")'),), (),
      ('get_root_tree_stubbed', 'get_root_tree_paginated'),
     ),
    )),
    ('session_list_warms_event_meta_off_path', (
     ('grep', 'main.py', (),
      ('def _schedule_session_event_meta_warm(',
       'await asyncio.to_thread(_warm_session_event_meta_roots_sync, pending)',
      ),
      ('_SESSION_DETAIL_WARM_EXECUTOR', 'async def _run_session_detail_warm_path(',
       'def _session_detail_projection_roots_for_page(', 'def _warm_session_detail_projection_roots(',
       'def _warm_session_detail_projection_roots_sync(', 'async def _warm_session_event_projections()',
      ),
     ),
     ('grep', 'main.py', (('def _schedule_session_event_meta_warm(', 'def _machine_nodes_enabled_cached('),),
      ('_warm_session_event_meta_roots(root_ids)',),
      ('_session_detail_snapshot_sync(', 'schedule_reconcile_if_needed', '_session_event_file_fingerprint(',
       '_session_event_meta_cache_fresh(',
      ),
     ),
     ('grep', 'main.py', (('def _session_event_meta_roots_for_page(', 'async def _warm_session_event_meta_roots('),),
      (), ('_session_event_file_fingerprint(',),
     ),
     ('grep', 'main.py', (('async def get_sessions(', '@app.post("/api/sessions/search-content")'),),
      ('_schedule_session_event_meta_warm(page)',), ('_session_event_meta(',),
     ),
    )),
    ('session_list_reads_user_prefs_once', (
     ('grep', 'main.py', (),
      ('def _session_list_user_prefs(', '_session_list_user_prefs_cache', '_SESSION_LIST_USER_PREFS_TTL_SECONDS'), (),
     ),
     ('grep', 'main.py', (('def _session_list_user_prefs(', '_GIT_STATUS_TTL_SECONDS'),),
      ('time.monotonic()', 'user_prefs.get_all()'), (),
     ),
     ('grep', 'main.py', (('async def get_sessions(', '@app.post("/api/sessions/search-content")'),),
      ('_session_list_user_prefs()',),
      ('await asyncio.to_thread(_session_list_user_prefs)', 'user_prefs.get_folder_view_enabled()',
       'user_prefs.get_session_sort()', 'user_prefs.get_session_status_sort()',
      ),
     ),
    )),
    ('session_detail_has_split_perf_timers', (
     ('grep', 'main.py', (('async def get_session(', '@app.get("/api/sessions/{session_id}/messages")'),),
      ('await _run_session_detail_hot_path(\n        "sessions.detail.worker"',
       'return await _json_bytes_response_async(tree)',
      ),
      ('await _run_hot_path(\n        "sessions.detail.worker"', 'session_manager.get_root_tree_stubbed',
       'perf.record("sessions.detail.worker"',
      ),
     ),
     ('grep', 'main.py',
      (('async def get_session(', '@app.get("/api/sessions/{session_id}/messages")'),
       ('cache_key_parts = tree.pop("_detail_response_cache_key_parts", None)', '    else:'),
      ),
      (), ('_session_event_meta(', '_session_event_file_fingerprint('),
     ),
     ('grep', 'main.py', (('def _json_bytes_response(', 'def _sessions_list_cache_get('),),
      ('separators=(",", ":")', 'Response(content=content, media_type="application/json")',
       'async def _json_bytes_response_async(', 'content = await asyncio.to_thread(',
      ),
      (),
     ),
     ('grep', 'main.py', (('def _session_detail_snapshot_sync(', 'def _floor_events_from_seq('),),
      ('perf.record("sessions.detail.root_id"', 'perf.record("sessions.detail.event_meta"',
       'perf.record("sessions.detail.tree"', 'perf.record("sessions.detail.strip_synthetic"',
       'perf.record("sessions.detail.reconcile_snapshot"', 'perf.record("sessions.detail.max_context_copy"',
       'perf.record("sessions.detail.total"', 'perf.record("sessions.detail.file_path"',
       'perf.record("sessions.detail.cache_marker"',
      ),
      (),
     ),
    )),
    ('session_hot_paths_use_dedicated_executor_with_queue_wait_metrics', (
     ('grep', 'main.py', (),
      ('_HOT_PATH_EXECUTOR = ThreadPoolExecutor(', 'max_workers=8', 'thread_name_prefix="hot-path"',
       '_SESSION_DETAIL_EXECUTOR = ThreadPoolExecutor(', 'thread_name_prefix="session-detail"',
       '_SESSION_LIST_EXECUTOR = ThreadPoolExecutor(', 'thread_name_prefix="session-list"',
      ),
      (),
     ),
     ('grep', 'main.py', (('async def _run_hot_path(', 'def _streaming_assistant_message_id('),),
      ('run_in_executor(\n            _HOT_PATH_EXECUTOR', 'async def _run_session_detail_hot_path(',
       'run_in_executor(\n            _SESSION_DETAIL_EXECUTOR', 'async def _run_session_list_hot_path(',
       'run_in_executor(\n            _SESSION_LIST_EXECUTOR', 'perf.record(f"{name}.queue_wait"',
       'perf.record(name,',
      ),
      (),
     ),
     ('grep', 'main.py', (('async def get_sessions(', '@app.post("/api/sessions/search-content")'),),
      ('await _run_session_list_hot_path(\n            "sessions.list.local_page_thread"',
       'await _run_session_list_hot_path(\n            "sessions.list.search_local_page.worker"',
       'await _run_session_list_hot_path(\n                    "sessions.list.remote.local_order_candidates.worker"',
       '"sessions.list.page_decorate.worker"',
      ),
      ('await asyncio.to_thread(_build_local_sessions_page_for_list',
       'await asyncio.to_thread(\n                _decorate_local_sidebar_sessions',
       'await asyncio.to_thread(\n            _decorate_local_sidebar_sessions',
       'await _run_hot_path(\n            "sessions.list.',
      ),
     ),
    )),
    ('sidebar_decoration_cache_uses_stable_session_version_key', (
     ('grep', 'main.py', (('def _decorate_local_sidebar_sessions(', 'def _sidebar_stats_payload('),),
      ('summary_version = session_store.summary_index_version()', 'sid,\n                summary_version,'),
      ('id(s),',),
     ),
    )),
    ('sidebar_summary_omits_worker_refs', (
     ('grep', 'session_store.py', (('def _build_summary_for_root(', 'def set_requirement_tags_projection('),),
      ('"worker_count"',), ('"workers"',),
     ),
     ('grep', 'session_store.py', (), ('def _sanitize_summary(', 'summary, cleaned = _sanitize_summary(summary)'), ()),
    )),
    ('summary_worker_count_uses_count_projection', (
     ('grep', 'session_store.py', (('def _worker_summary_count()', 'def _refresh_summaries_for_cwd_from('),),
      ('worker_store.worker_count("")',), ('worker_store.list_workers("")',),
     ),
     ('grep', 'stores/worker_store.py', (),
      ('_worker_count_cache', '_registry_cache_signature', '_registry_cache', '_workers_dir_cache',
       'return _merge_activity(deepcopy(_registry_cache))', '_WORKER_COUNT_HOT_TTL_SECONDS',
       'now < _worker_count_cache_until', 'def worker_count(', '_worker_count_cache.clear()',
      ),
      (),
     ),
    )),
    ('summary_sidecar_stat_only_for_unchanged_summary', (
     ('grep', 'session_store.py', (), ('_summary_sidecar_write_queue', 'def _schedule_summary_sidecar_write('), ()),
     ('grep', 'session_store.py', (('def _upsert_summary(', 'def _seen_cursor_path('),),
      ('sidecar_current = True', 'if not summary_changed:', 'root_mtime_ns=root_mtime_ns',
       'if summary_changed or not sidecar_current:', 'if sync_sidecar:', '_write_summary_file(',
       '_schedule_summary_sidecar_write(',
      ),
      (),
     ),
     ('grep', 'session_store.py', (('def write_session_full(', 'def list_sessions('),),
      ('root_mtime_ns=file_signature[3] if file_signature is not None else None',
       'sync_sidecar=bool(root.get("forks"))',
      ),
      (),
     ),
    )),
    ('root_resolution_consults_loaded_index_before_filesystem_shortcut', (
     ('grep', 'session_store.py', (('def _resolve_root_id(', 'def _session_path('),),
      ('_root_file_path(sid).exists()', '_ensure_index()', '_loaded_root_id_for(sid)'), (),
     ),
     ('grep', 'session_store.py', (('def _loaded_root_id_for(', 'def _resolve_root_id('),),
      ('if not _index_loaded:', 'sid in _root_index_signatures', '_fork_index.get(sid)'), (),
     ),
     ('ordered', 'session_store.py', (('def _resolve_root_id(', 'def _session_path('),),
      (('_loaded_root_id_for(sid)',), ('_root_file_path(sid).exists()',)),
     ),
     ('ordered', 'session_store.py', (('def _resolve_root_id(', 'def _session_path('),),
      (('_root_file_path(sid).exists()',), ('_ensure_index()',)),
     ),
    )),
    ('summary_index_skips_empty_projection_scan', (
     ('grep', 'session_store.py', (),
      ('def _projection_snapshot()', 'def _start_summary_projection_repair(',
       '_summary_projection_repair_lock = threading.Lock()', '_summary_projection_repair_running = False',
      ),
      ('def _has_projection_snapshot()',),
     ),
     ('grep', 'session_store.py', (('def _start_summary_projection_repair()', 'def summary_version()'),),
      ('if _summary_projection_repair_running:', '_summary_projection_repair_running = True',
       '_summary_projection_repair_running = False', 'finally:', 'updates: dict[str, dict] = {}',
       'projection_snapshot = _projection_snapshot()',
      ),
      ('_requirement_tags_for_session(sid)', '_markers_for_session(sid)'),
     ),
     ('count_eq', 'session_store.py', (('def _start_summary_projection_repair()', 'def summary_version()'),),
      '_summary_index_version += 1', 1,
     ),
     ('grep', 'session_store.py',
      (('def _do_build_summary_index_unsafe()', 'def _refresh_summaries_for_cwd('),
       ('cached_summaries = _load_summary_index_cache(', '# Trees migrated in Pass 2'),
      ),
      ('_start_summary_projection_repair()', 'return'), ('if _has_projection_snapshot()',),
     ),
     ('grep', 'session_store.py', (('def _do_build_summary_index_unsafe()', 'def _refresh_summaries_for_cwd('),),
      ('projection_snapshot = _projection_snapshot()',
       'organization_projection = session_organization_store.enrichment_projection()', '_build_summary_for_root(',
       'organization_projection,', '_start_summary_projection_repair()',
      ),
      ('_summary_has_projection(', 'summary_projection_present', 'if _has_projection_snapshot()',
       'summary_items = list(_summary_index.items())',
      ),
     ),
    )),
    ('summary_index_validates_missing_summary_before_provider_context', (
     ('grep', 'session_store.py', (('def _do_build_summary_index_unsafe()', 'def _refresh_summaries_for_cwd('),),
      ('provider_ctx: Optional[dict] = None',), (),
     ),
     ('ordered', 'session_store.py', (('def _do_build_summary_index_unsafe()', 'def _refresh_summaries_for_cwd('),),
      (('raw = json.loads(fpath.read_text',), ('if not isinstance(raw, dict) or "id" not in raw:',),
       ('provider_ctx = _provider_backfill_context()',), ('data = _migrate_session(raw, provider_ctx)',),
      ),
     ),
    )),
    ('summary_index_indexes_seen_sidecars_once', (
     ('grep', 'session_store.py', (('def _do_build_summary_index_unsafe()', 'def _refresh_summaries_for_cwd('),),
      ('seen_cursor_ids: set[str] = set()', 'for storage_dir in _session_storage_dirs():',
       'entries = list(storage_dir.iterdir())', 'read_seen_cursors(sid) if sid in seen_cursor_ids else {}',
       '_summary_index_cache_fingerprint(', '_load_summary_index_cache(summary_cache_fingerprint)',
       '_write_summary_index_cache(summary_cache_fingerprint, summaries)',
      ),
      ('.glob("*.summary.json")', '.glob("*.seen.json")'),
     ),
     ('grep', 'session_store.py', (), ('"skipped_root_ids"',), ()),
    )),
    ('summary_index_cache_is_sidecar', (
     ('grep', 'session_store.py', (), ('def _summary_index_cache_path()', '".summary-index.json"'), ()),
     ('grep', 'session_store.py', (('_SIDECAR_JSON_SUFFIXES = (', 'def _is_sidecar_json'),),
      ('".summary-index.json"',), (),
     ),
    )),
    ('session_store_sessions_dir_is_env_aware_cached', (
     ('grep', 'session_store.py', (),
      ('_SESSIONS_DIR: Path | None = None', '_SESSIONS_DIR_READY = False',
       '_SESSIONS_DIR_READY_LOCK = threading.Lock()',
      ),
      (),
     ),
     ('grep', 'session_store.py', (('def _sessions_dir()', 'def _ensure_dir()'),),
      ('resolved = ba_home() / "sessions"', 'if _SESSIONS_DIR == resolved:', '_reset_home_scoped_caches()'), (),
     ),
     ('grep', 'session_store.py', (('def _ensure_dir()', '# ── Fork index'),),
      ('sessions_dir = _sessions_dir()', 'if _SESSIONS_DIR_READY:\n        return',
       'sessions_dir.mkdir(parents=True, exist_ok=True)', '_SESSIONS_DIR_READY = True',
      ),
      (),
     ),
    )),
    ('event_journal_watch_path_uses_cached_sessions_dir', (
     ('grep', 'event_journal.py', (), ('def _session_artifacts_dir(', 'session_store.session_file_path('), ()),
     ('grep', 'event_journal.py', (('def _read_appended_entries(', 'def read_events('),),
      ('_session_artifacts_dir(session_id) / "events.jsonl"', 'os.SEEK_END'), ('ba_home()', '.exists(', '.stat('),
     ),
    )),
    ('run_state_emit_debug_logging_is_gated', (
     ('grep', 'turn_manager.py',
      (('def _dbg_runstate(', 'def audit_running_discrepancies('),),
      ('logger.isEnabledFor(logging.DEBUG)', 'logger.debug('), ('logger.info(',),
     ),
     ('grep', 'turn_manager.py',
      (('def _run_state_touch(', '# ======================================================================'),),
      ('await self._c.broadcast_session',), ('logger.info(',),
     ),
    )),
    ('startup_session_search_rebuild_skips_persisted_index', (
     ('grep', 'main.py', (('async def on_startup()', 'async def on_shutdown()'),),
      ('session_search_index.needs_rebuild()',), (),
     ),
    )),
    ('event_projections_do_not_eager_warm_detail_snapshots', (
     ('grep', 'main.py', (), (),
      ('def _session_event_projection_warm_roots(', 'def _warm_session_detail_projection_roots_sync(',
       'async def _warm_session_event_projections()', '_SESSION_DETAIL_WARM_EXECUTOR',
       'async def _run_session_detail_warm_path(',
      ),
     ),
     ('grep', 'main.py', (('async def on_shutdown()', '# Internal Endpoints'),),
      ('_SESSION_DETAIL_EXECUTOR.shutdown(',), ('_SESSION_DETAIL_WARM_EXECUTOR.shutdown(',),
     ),
     ('grep', 'main.py', (('async def on_startup()', 'async def on_shutdown()'),), (),
      ('startup-session-event-meta-projection-warm', 'session_event_projection_warm'),
     ),
     ('grep', 'main.py', (('def _schedule_session_event_meta_warm(', 'def _machine_nodes_enabled_cached('),), (),
      ('_session_detail_snapshot_sync(', 'session_manager.schedule_reconcile_if_needed'),
     ),
    )),
    ('render_hydrate_worker_fingerprint_is_batched', (
     ('grep', 'render_tree_hydrate.py',
      (('            pre_worker_fingerprint = (', '            for raw in orphan_rows:'),),
      ('pre_worker_fingerprint is not None',), ('before_worker',),
     ),
     ('count_eq', 'render_tree_hydrate.py',
      (('            pre_worker_fingerprint = (', '            for raw in orphan_rows:'),),
      '_message_timeline_fingerprint(m)', 2,
     ),
    )),
    ('project_match_rebuild_skips_unchanged_session_state', (
     ('grep', 'main.py', (('async def _project_match_warm_loop()', 'def _ensure_project_match_warm_task()'),),
      ('fingerprint = None', 'rebuild_index,\n                fingerprint,', 'result.get("fingerprint")',
       'result.get("rebuilt") is False',
      ),
      (),
     ),
     ('grep', 'project_match/worker.py', (),
      ('def sessions_fingerprint()', 'previous_fingerprint is not None and fingerprint == previous_fingerprint',
       '{"rebuilt": False, "fingerprint": fingerprint}',
      ),
      (),
     ),
    )),
    ('stubbed_tree_cache_key_does_not_scan_message_events', (
     ('grep', 'session_manager.py', (('def _tree_stub_cache_key(', 'def _build_stubbed_tree('),),
      ('render_seq_by_sid = event_ingester.render_seq_by_sid(rid)',),
      ('msg.get("events")', 'event_shape', 'root_events_version'),
     ),
    )),
    ('worker_panel_anchor_derivation_is_cached', (
     ('grep', 'render_stub.py', (), ('_PANEL_ANCHOR_CACHE', 'anchors = _panel_anchors(msg, manager_events, workers)'),
      (),
     ),
     ('grep', 'render_stub.py', (('def _panel_anchors(', 'def timeline_events('),),
      ('cached.get("key") == key', 'return anchors'), (),
     ),
     ('grep', 'session_manager.py', (('def append_native_event(', 'def replace_native_event('),),
      ('invalidate_panel_anchor_cache(m)',), (),
     ),
     ('grep', 'session_manager.py', (('def replace_native_event(', 'def set_agent_sid_on_msg('),),
      ('invalidate_panel_anchor_cache(m)',), (),
     ),
     ('grep', 'session_store.py', (('def _strip_volatile_from_tree(', 'def copy_persistable_tree('),),
      ('"_panel_anchor_cache"', 'panel_anchor_caches'), (),
     ),
    )),
    ('startup_recovery_defers_cold_runs', (
     ('grep', 'main.py', (('async def _recover_in_flight_task()', 'async def _housekeeping_task()'),),
      ('live = [r for r in recovered if bool(r.get("alive"))]',
       'cold = [r for r in recovered if not bool(r.get("alive"))]', '_enqueue_recovered_cold_runs(cold)',
      ),
      (),
     ),
    )),
    ('startup_recovery_gate_opens_after_live_before_background_recovery', (
     ('ordered', 'main.py', (('async def _recover_in_flight_task()', 'async def _housekeeping_task()'),),
      (('startup_recovery_gate.mark_recovery_sessions_registered()',),
       ('await integrate_recovered_runs(coordinator, batch)',),
      ),
     ),
     ('ordered', 'main.py', (('async def _recover_in_flight_task()', 'async def _housekeeping_task()'),),
      (('await integrate_recovered_runs(coordinator, batch)',), ('_enqueue_recovered_cold_runs(cold)',),
       ('await _re_enqueue_queued_prompts()',), ('coordinator.turn_manager.reconcile_detached_background()',),
       ('startup_recovery_gate.mark_recovery_done()',),
      ),
     ),
    )),
    ('hydration_uses_local_projection_not_extension_backend', (
     ('grep', 'session_manager.py', (('    def _derive_current_todos_from_events_jsonl(', '    def _cached('),),
      ('session_local_projection.project_event_fields(',), ('session_event_extensions', 'extension_backend_loader'),
     ),
    )),
    ('session_event_extension_callbacks_are_worker_only', (
     ('grep', 'session_event_extensions.py', (('def project_event(', 'def _apply_builtin_event('),), (),
      ('invoke_extension_backend_sync',),
     ),
     ('grep', 'session_event_extensions.py', (('def apply_event(', 'def _apply_event_locked('),), (),
      ('invoke_extension_backend_sync',),
     ),
     ('grep', 'session_event_extensions.py', (('def _run_extension_hook_job(', 'def _run_builtin_todos_job('),),
      ('invoke_extension_backend_sync',), (),
     ),
    )),
    ('session_event_apply_event_uses_cached_hook_snapshot', (
     ('grep', 'session_event_extensions.py', (('def apply_event(', 'def _apply_event_locked('),),
      ('hook_snapshot_nonblocking()',), ('hook_snapshot()', 'session_event_hook_specs()', '_builtin_todos_enabled()'),
     ),
    )),
    ('requirement_tag_refresh_is_off_startup_loop', (
     ('grep', 'event_bus_subscribers.py',
      (('async def _refresh_requirement_tags(', 'async def _apply_requirement_tags_projection('),),
      ('await asyncio.to_thread(_refresh_requirement_tags_sync)', 'ModuleNotFoundError'), (),
     ),
     ('grep', 'main.py', (('async def on_startup()', 'async def on_shutdown()'),), ('ModuleNotFoundError',),
      ('name="requirement-tags-startup-refresh"', 'type="requirement_tags.refresh_requested"',
       'await event_bus.publish(BusEvent(\\n            type="requirement_tags.refresh_requested"',
      ),
     ),
    )),
    ('machine_nodes_readiness_check_is_off_startup_loop', (
     ('grep', 'main.py', (('async def on_startup()', 'async def on_shutdown()'),),
      ('async def _start_node_offset_loop_if_ready()',
       'await asyncio.to_thread(\n                extension_store.is_extension_runtime_ready',
       'name="node-offset-flush-startup"',
      ),
      (),
     ),
    )),
    ('sessions_route_does_not_runtime_check_machine_nodes', (
     ('grep', 'main.py', (('@app.get("/api/sessions")', '@app.get("/api/sessions/{session_id}")'),),
      ('connected_worker_node_ids_snapshot()',),
      ('_ns.snapshot()', 'sessions.list.node_snapshot', '_builtin_extension_runtime_ready_fast',
       '_builtin_extension_runtime_ready(',
      ),
     ),
     ('grep', 'main.py', (('def _machine_nodes_enabled_cached(', 'def _sessions_list_response('),),
      ('asyncio.create_task(_refresh())',
       'await asyncio.to_thread(\n                        _builtin_extension_runtime_ready', 'return cached[1]',
      ),
      (),
     ),
    )),
    ('sessions_route_uses_cached_remote_node_sessions', (
     ('grep', 'main.py', (),
      ('_REMOTE_SESSIONS_CACHE_TTL_SECONDS = 2.0', 'def _remote_sessions_cache_get(\n    node_id: str,',
       'limit: int | None = None', 'def _schedule_remote_sessions_refresh(node_id: str)',
       'async def _fetch_remote_sessions_live(node_id: str)',
      ),
      (),
     ),
     ('grep', 'main.py', (('async def _remote_sessions_for_sidebar(', 'def _session_list_user_prefs('),),
      ('sessions.list.remote_cache.hit', 'sessions.list.remote_cache.stale', 'sessions.list.remote_cache.miss'), (),
     ),
     ('grep', 'main.py', (('@app.get("/api/sessions")', '@app.get("/api/sessions/{session_id}")'),),
      ('_remote_sessions_cache_version_snapshot() if connected else 0', 'with perf.timed("sessions.list.remote")',
       '_remote_sessions_for_sidebar(nid)', 'rs["node_id"] = nid',
      ),
      (),
     ),
    )),
    ('connected_session_list_defers_cold_sidebar_projections', (
     ('grep', 'virtual_session_store.py', (), ('def list_recent_cached(',), ()),
     ('grep', 'main.py',
      (('def _remote_sessions_for_sidebar_cached(', 'def _schedule_virtual_sessions_recent_refresh('),),
      ('sessions.list.remote_cache.deferred_miss', '_schedule_remote_sessions_refresh(node_id)'), (),
     ),
     ('grep', 'main.py', (('def _schedule_virtual_sessions_recent_refresh(', 'def _session_list_user_prefs('),),
      ('asyncio.to_thread(\n            virtual_session_store.list_recent,',), (),
     ),
     ('grep', 'main.py', (('@app.get("/api/sessions")', '@app.get("/api/sessions/{session_id}")'),),
      ('sessions.list.virtual.cached_first_page',
       '_remote_sessions_for_sidebar_cached(\n                        nid,', 'limit=max(offset + limit, 1)',
       'deferred_sidebar_projection and not appended_virtual_sessions and not appended_remote_sessions',
       'projected_first_page_sessions', 'sessions.list.projected_first_page_merge',
       '_sessions_list_response(\n                    json.dumps(',
      ),
      (),
     ),
     ('ordered', 'main.py', (('@app.get("/api/sessions")', '@app.get("/api/sessions/{session_id}")'),),
      (('sessions.list.projected_first_page_merge',), ('with perf.timed("sessions.list.filter_sort")',)),
     ),
    )),
    ('local_session_first_page_prefers_cached_virtual_projection', (
     ('grep', 'main.py', (('def _build_local_sessions_page_for_list(', 'async def _sidebar_search_scores('),),
      ('virtual_session_store.list_recent_cached(',), (),
     ),
     ('ordered', 'main.py', (('def _build_local_sessions_page_for_list(', 'async def _sidebar_search_scores('),),
      (('virtual_session_store.list_recent_cached(',), ('virtual_session_store.list_recent(',)),
     ),
    )),
    ('submit_team_message_sync_store_work_off_loop', (
     ('grep', 'orchestrator.py', (('async def submit_team_message(', '    def _resolve_delegation_run_config('),),
      ('sender, target = await asyncio.to_thread(\n            team_messaging.validate_message_route',
       'metadata = await asyncio.to_thread(\n            team_messaging.build_message_metadata',
       'queue_item = await asyncio.to_thread(\n            team_messaging.queue_payload',
       'await asyncio.to_thread(\n                session_manager.add_queued_prompt',
       'cli_prompt = await asyncio.to_thread(\n            team_messaging.format_team_message_prompt',
       'await asyncio.to_thread(\n                session_manager.remove_queued_prompt',
       'await self.submit_prompt_async(target_session_id, prompt_params)',
      ),
      ('session_manager.add_queued_prompt(', 'cli_prompt = team_messaging.format_team_message_prompt(',
       'self.submit_prompt(target_session_id, {',
      ),
     ),
    )),
    ('default_session_page_uses_visible_order_cache', (
     ('grep', 'session_store.py', (), ('def sidebar_session_summary_page(',), ()),
     ('grep', 'main.py', (('def _local_session_page_for_sidebar_preserving_order(', 'def _root_session_file_path('),),
      ('_can_page_default_local_visible_order(', 'sessions.list.local.visible_order_page',
       'session_store.sidebar_session_summary_page(',
       'sessions.list.local.visible_order_page.order_generation',
       'sessions.list.local.visible_order_page.visibility_generation',
      ),
      (),
     ),
     ('ordered', 'main.py',
      (('def _local_session_page_for_sidebar_preserving_order(', 'def _root_session_file_path('),),
      (('sessions.list.local.visible_order_page',), ('sessions.list.local.ordered_ids',)),
     ),
    )),
    ('session_search_uses_bounded_candidate_window', (
     ('grep', 'main.py', (), ('_SESSION_LIST_SEARCH_MIN_CANDIDATES = 200',), ()),
     ('grep', 'main.py', (('def _session_search_candidate_limit(', '@app.get("/api/sessions")'),),
      ('max(offset + limit, _SESSION_LIST_SEARCH_MIN_CANDIDATES)',), (),
     ),
     ('grep', 'main.py', (('async def _sidebar_search_scores(', '@app.get("/api/sessions")'),),
      ('session_store.SEARCH_FIELD_CONTENT in selected_search_fields',), (),
     ),
     ('grep', 'main.py', (('@app.get("/api/sessions")', '@app.post("/api/sessions/search-content")'),),
      ('content_limit=_session_search_candidate_limit(offset, limit)',
       'cached_response = _sessions_list_cache_get(cache_key)', 'sessions.list.search_local_page.worker',
      ),
      ('content_limit=max(offset + limit, 1)', 'cache_response = not ('),
     ),
     ('ordered', 'main.py', (('@app.get("/api/sessions")', '@app.post("/api/sessions/search-content")'),),
      (('sessions.list.search_local_page.worker',), ('with perf.timed("sessions.list.remote")',)),
     ),
    )),
    ('session_list_filter_sort_keeps_only_page_candidates', (
     ('grep', 'main.py', (('def _filter_sort_page_for_list(', 'def _filter_sessions_for_list_preserving_order('),),
      ('heapq.heapreplace(selected, item)', 'heapq.heappush(selected, item)', 'total += 1'),
      ('heapq.nlargest(', 'selected.append('),
     ),
    )),
    ('startup_warms_virtual_session_summaries_off_loop', (
     ('grep', 'main.py', (('async def on_startup()', 'async def on_shutdown()'),),
      ('"virtual_session_summaries_warm"', '"startup_tasks.virtual_session_summaries_warm"',
       'virtual_session_store.list_all', 'name="startup-virtual-session-summaries-warm"',
      ),
      (),
     ),
    )),
    ('startup_warms_recent_git_statuses_off_hot_path', (
     ('grep', 'main.py', (), ('_GIT_STATUS_STARTUP_WARM_LIMIT = 8',), ()),
     ('grep', 'main.py', (('async def _warm_recent_git_statuses()', 'def _shutdown_kill_runners_flag()'),),
      ('await asyncio.to_thread(project_store.list_projects)', 'node_id != "primary"',
       'await _cached_git_status(node_id, cwd)', 'warmed >= _GIT_STATUS_STARTUP_WARM_LIMIT',
      ),
      (),
     ),
     ('grep', 'main.py', (('async def on_startup()', 'async def on_shutdown()'),),
      ('"git_status_warm"', '"startup_tasks.git_status_warm"', '_warm_recent_git_statuses', 'in_thread=False',
       'name="startup-git-status-warm"',
      ),
      (),
     ),
    )),
    ('session_organization_refresh_is_coalesced_background_work', (
     ('grep', 'main.py',
      (('async def _broadcast_session_organization_changed(', 'async def _apply_initial_session_folder('),),
      ('_session_organization_refresh_pending = True', 'asyncio.create_task(_refresh_loop())',
       'await asyncio.to_thread(session_store.refresh_organization_projection, session_ids)',
       'if _session_organization_refresh_task is not None and not _session_organization_refresh_task.done()',
      ),
      (),
     ),
    )),
    ('get_session_strips_synthetic_events_off_loop', (
     ('grep', 'main.py', (), ('def _tree_has_loaded_events(',), ()),
     ('grep', 'main.py', (('async def get_session(', '@app.get("/api/sessions/{session_id}/messages")'),), (),
      ('_strip_synthetic_events_from_tree(tree)',),
     ),
     ('grep', 'main.py', (('def _session_detail_snapshot_sync(', 'def _floor_events_from_seq('),),
      ('if _tree_has_loaded_events(tree):', '_strip_synthetic_events_from_tree(tree)', 'strip_ms'), (),
     ),
    )),
    ('session_detail_response_bytes_are_cached', (
     ('grep', 'main.py', (),
      ('_session_detail_response_cache', '_SESSION_DETAIL_RESPONSE_CACHE_MAX = 64', 'def _session_detail_cache_get(',
       'def _session_detail_cache_put(', 'def _session_detail_response_cache_key_sync(',
       'def _session_detail_cache_put_async(',
      ),
      ('_SESSION_DETAIL_RESPONSE_TTL_SECONDS',),
     ),
     ('grep', 'main.py', (('def _session_detail_cache_get(', 'def _session_detail_cache_put('),), (),
      ('time.monotonic()',),
     ),
     ('grep', 'main.py', (('async def get_session(', '@app.get("/api/sessions/{session_id}/messages")'),),
      ('_session_detail_cache_get(cache_key)', '_session_detail_response_cache_latest.get(simple_cache_key)',
       'if cached_full_key is not None:', '_session_reconcile_snapshot_and_schedule', 'include_cache_key=True',
       'await _session_detail_cache_put_async(cache_key, tree)',
      ),
      (),
     ),
    )),
    ('stubbed_tree_cache_covers_broad_session_loads', (
     ('grep', 'session_manager.py', (), ('self._tree_stub_cache_max = 256',), ()),
    )),
    ('run_recovery_finalize_session_manager_calls_are_off_loop', (
     ('grep', 'run_recovery.py',
      (('async def _finalize_when_done(',
        '# ============================================================================',
       ),
      ),
      ('await asyncio.to_thread(\n            _recovery_target_snapshot',
       'await asyncio.to_thread(\n                    session_manager.set_msg_recovering',
      ),
      ('session_manager.get(persist_sid)', 'session_manager.set_msg_recovering(persist_sid'),
     ),
     ('grep', 'run_recovery.py', (('async def _integrate_one(', 'async def _retry_recovered_run('),),
      ('await _to_thread_joined(\n                coordinator.turn_manager.run_state_add',),
      ('\n            coordinator.turn_manager.run_state_add(',),
     ),
     ('grep', 'run_recovery.py', (('async def _retry_recovered_run(', 'def _cleanup_active_run_id('),),
      ('await asyncio.to_thread(\n        coordinator.turn_manager.run_state_add',),
      ('\n    coordinator.turn_manager.run_state_add(',),
     ),
    )),
    ('delegation_run_state_mutations_run_off_loop', (
     ('grep', 'orchs/manager/_delegation.py', (('async def run_delegation(', 'async def run_delegation_locked('),),
      ('await asyncio.to_thread(\n        coordinator.turn_manager.run_state_add',
       'await asyncio.to_thread(\n            coordinator.turn_manager.run_state_remove',
      ),
      ('\n    coordinator.turn_manager.run_state_add(', '\n        coordinator.turn_manager.run_state_remove('),
     ),
     ('grep', 'orchs/manager/_delegation.py',
      (('async def run_delegation(', None), ('async def run_delegation_locked(', 'def _remove_run_id() -> None:')),
      ('await asyncio.to_thread(\n            coordinator.turn_manager.run_state_set_pid',),
      ('\n        coordinator.turn_manager.run_state_set_pid(',),
     ),
    )),
    ('run_recovery_summarizes_repeated_skip_logs', (
     ('grep', 'run_recovery.py', (),
      ('class _RecoveryLogSummary:', 'summary.record_skip("missing target_message_id", run_id)',
       'summary.record_tombstoned(reason, run_id)', 'summary.emit()',
       'integrate_recovered_runs: skip %s (missing target_message_id)',
       'integrate_recovered_runs: skipped %d run(s): %s%s',
      ),
      (),
     ),
    )),
    ('provider_start_run_is_off_loop_everywhere', (
     ('grep', 'orchs/manager/_delegation.py', (),
      ('await asyncio.to_thread(\n                    provider.start_run,',
       'await asyncio.to_thread(\n                    session_manager.flush_root_persist, app_session_id,',
      ),
      ('\n            provider.start_run(', 'session_manager.flush_pending_persists'),
     ),
     ('grep', 'run_recovery.py', (),
      ('await asyncio.to_thread(\n        provider.start_run,',
       'await asyncio.to_thread(session_manager.flush_pending_persists)',
      ),
      ('\n    provider.start_run(',),
     ),
     ('grep', 'node_rpc_handlers.py', (),
      ('await asyncio.to_thread(\n                provider.start_run,',
       'await asyncio.to_thread(session_manager.flush_root_persist, root_id)',
      ),
      ('\n        provider.start_run(', 'session_manager.flush_pending_persists'),
     ),
    )),
    ('extension_backend_get_skips_body_stream', (
     ('grep', 'extension_backend_loader.py', (), ('_METHODS_WITH_REQUEST_BODY = {"POST", "PUT", "PATCH", "DELETE"}',),
      (),
     ),
     ('grep', 'extension_backend_loader.py',
      (('async def dispatch_extension_backend_request(', 'async def invoke_extension_backend('),),
      ('method = str(getattr(request, "method", "POST") or "POST").upper()',
       'if method in _METHODS_WITH_REQUEST_BODY', 'else b""',
      ),
      (),
     ),
    )),
    ('extension_backend_invoke_has_split_perf_timers', (
     ('grep', 'extension_backend_loader.py', (), ('_EMPTY_B64 = ""',), ()),
     ('grep', 'extension_backend_loader.py',
      (('async def _invoke_backend(', 'async def dispatch_extension_backend_request('),),
      ('extension.backend.invoke.payload', 'extension.backend.invoke.handle', 'extension.backend.invoke.timeout',
       'extension.backend.invoke.roundtrip', 'extension.backend.invoke.decode', 'extension.backend.invoke.response',
       'body_b64 = (', 'if body_bytes',
      ),
      (),
     ),
     ('grep', 'extension_backend_loader.py',
      (('async def dispatch_extension_backend_request(', 'async def invoke_extension_backend('),),
      ('else _EMPTY_B64',), (),
     ),
    )),
    ('builtin_extension_core_dispatch_precedes_backend_spec_lookup', (
     ('ordered', 'extension_api.py',
      (('async def dispatch_backend_extension(', 'async def _dispatch_core_builtin_backend('),),
      (('_dispatch_core_builtin_backend(',), ('_backend_entrypoint_spec_async(',)),
     ),
     ('grep', 'extension_api.py',
      (('async def _dispatch_core_builtin_backend(', 'async def _dispatch_machine_nodes_core_backend('),),
      ('roles, enabled = await _CORE_ROLE_EXECUTOR.run(', '_core_routing_projection', 'owner == extension_id',
       'owned_roles = {name for name, owner in roles.items()',
       '("team-orchestration", _dispatch_team_orchestration_core_backend)',
       '("scheduler", _dispatch_scheduler_core_backend)', '_dispatch_scheduler_core_backend',
       '("routines", _dispatch_routines_core_backend)', '_dispatch_routines_core_backend',
       '("project-structure", _dispatch_project_structure_core_backend)', 'if role not in owned_roles:',
       'if not enabled:',
      ),
      (),
     ),
     ('grep', 'extension_api.py', (('def _core_routing_projection(', 'async def shutdown_hot_path_executors('),),
      ('extension_store.is_extension_enabled_cached(extension_id)',), (),
     ),
     ('grep', 'extension_api.py',
      (('async def _dispatch_routines_core_backend(', 'async def _dispatch_scheduler_core_backend('),),
      ('request.method != "GET" or path != "routines"', 'task_store.list_for_project'), ('extension_backend_loader',),
     ),
     ('grep', 'extension_api.py',
      (('async def _dispatch_scheduler_core_backend(', 'async def _dispatch_team_orchestration_core_backend('),),
      ('request.method != "GET"', 'parts[0] != "sessions"', 'parts[2] != "schedules"',
       '_run_scheduler_read(app_session_id)',
      ),
      ('extension_backend_loader',),
     ),
     ('grep', 'extension_api.py', (('def _scheduler_session_snapshot(', 'def _local_node_id_or_primary_cached('),),
      ('session_manager.manager.get', 'session.get("id") != app_session_id', 'schedule_store.list_for_session',
       '_SCHEDULER_READ_EXECUTOR',
      ),
      (),
     ),
     ('grep', 'extension_api.py', (), ('name="extension.scheduler.read"',), ()),
     ('grep', 'bounded_async_executor.py', (),
      ('f"{self._name}.queue_wait"', 'f"{self._name}.run"', 'f"{self._name}.rejected"'), (),
     ),
     ('grep', 'extension_api.py',
      (('async def _dispatch_team_orchestration_core_backend(', 'async def _dispatch_machine_nodes_core_backend('),),
      ('request.method == "GET" and path == "workers"', 'request.method == "GET" and path == "pending_approvals"',
       'team_orchestration_read.workers_response_bytes', 'pending_approvals.list_pending',
      ),
      (),
     ),
     ('grep', 'extension_api.py',
      (('async def _dispatch_project_structure_core_backend(', '@router.post("/install")'),),
      ('request.method == "GET" and path == "project-updates/total"',
       'request.method != "POST" or path != "project-updates/counts-batch"',
       'project_update_store.peek_total_unseen()', 'project_update_store.peek_unseen_counts(project_ids)',
       'await asyncio.to_thread(project_update_store.total_unseen)',
       'await asyncio.to_thread(project_update_store.unseen_counts, project_ids)',
      ),
      (),
     ),
    )),
    ('project_update_total_is_maintained_projection', (
     ('grep', 'project_update_store.py', (),
      ('_total_unseen_count = 0', '_counts_version = 0', 'def version_token(', 'def warm_counts(',
       'def _project_path(project_id: str, *, create_dir: bool = True)',
       '_project_path(project_id, create_dir=False)',
      ),
      (),
     ),
     ('grep', 'project_update_store.py', (('def _ensure_counts_locked(', 'def _set_count_locked('),),
      ('_total_unseen_count = total', '_read_entries_path_locked(path)'), ('_read_entries_locked(path.stem)',),
     ),
     ('grep', 'project_update_store.py', (('def _set_count_locked(', 'def append('),),
      ('if count == previous:\n        return', '_total_unseen_count += count - previous',
       '_total_unseen_count -= previous', '_counts_version += 1',
      ),
      (),
     ),
     ('grep', 'project_update_store.py', (('def append(', 'def list_unseen('),),
      ('_set_count_locked(project_id, _unseen_counts.get(project_id, 0) + 1)',), (),
     ),
     ('grep', 'project_update_store.py', (('def mark_seen(', 'def list_all('),),
      ('_set_count_locked(project_id, _unseen_counts.get(project_id, 0) - count)',), (),
     ),
     ('grep', 'project_update_store.py', (('def total_unseen(', 'def mark_seen('),), ('return _total_unseen_count',),
      ('sum(_unseen_counts.values())',),
     ),
     ('grep', 'main.py', (('async def on_startup()', 'async def on_shutdown()'),),
      ('"project_update_counts_warm"', 'project_update_store.warm_counts',
       'name="startup-project-update-counts-warm"', '"pending_node_projection_warm"',
       '"startup_tasks.pending_node_projection_warm"', 'name="startup-pending-node-projection-warm"',
      ),
      (),
     ),
     ('grep', 'main.py',
      (('async def on_startup()', 'async def on_shutdown()'),
       ('def _warm_pending_node_projection()', 'asyncio.create_task('),
      ),
      ('node_link.public_pending_nodes()',), (),
     ),
    )),
    ('builtin_feature_enabled_has_cached_projection', (
     ('grep', 'extension_store.py', (),
      ('_BUILTIN_FEATURE_CACHE', 'def is_builtin_feature_enabled_cached(', '_STORE_FINGERPRINT_CACHE',
       '_STORE_FINGERPRINT_TTL_SECONDS', '_STORE_PATH',
      ),
      (),
     ),
     ('grep', 'extension_store.py', (('def _store_path(', 'def store_fingerprint('),),
      ('_STORE_PATH_HOME_KEY', '_STORE_PATH_HOME_KEY != home_key', 'ba_home()'), (),
     ),
     ('grep', 'extension_store.py', (('def is_builtin_feature_enabled_cached(', 'def is_extension_runtime_ready('),),
      ('fingerprint = store_fingerprint()', '_BUILTIN_FEATURE_CACHE.get(extension_id)',
       'is_builtin_feature_enabled(extension_id)',
      ),
      (),
     ),
     ('grep', 'extension_store.py', (('def store_fingerprint(', 'def _refresh_store_fingerprint_cache('),),
      ('_STORE_FINGERPRINT_CACHE_LOCK', 'hashlib.sha256(path.read_bytes()).hexdigest()', 'return cached[1]'), (),
     ),
     ('grep', 'extension_store.py', (('def _write_store_unlocked(', 'def _merge_store_for_save('),),
      ('_refresh_store_fingerprint_cache(path)',), (),
     ),
     ('ordered', 'extension_store.py', (('def _write_store_unlocked(', 'def _merge_store_for_save('),),
      (('os.replace(tmp_name, path)',), ('_refresh_store_fingerprint_cache(path)',)),
     ),
    )),
    ('extension_list_reconciliation_is_off_loop', (
     ('grep', 'extension_api.py', (('async def list_extensions(', '@router.get("/builtin-ids")'),),
      ('fingerprint = await _extension_store_fingerprint_async()', 'cache_key = (fingerprint, include_hidden)',
       '_projection_response_cache_get("list", cache_key)',
       'await asyncio.to_thread(\n        extension_store.list_extensions_with_reconciliation',
       '_projection_response_cache_put("list", cache_key, {"extensions": extensions})',
      ),
      ('extensions, changed = extension_store.list_extensions_with_reconciliation',),
     ),
    )),
    ('internal_communication_worker_lookup_is_off_loop', (
     ('grep', 'main.py', (('async def _resolve_communication_target(', '@app.post("/api/internal/ask")'),),
      ('await asyncio.to_thread(_find_worker_by_agent_session_id',
       'await asyncio.to_thread(\n        _pick_pool_worker_for_sender',
      ),
      (),
     ),
     ('grep', 'main.py',
      (('async def _ask_continue_and_expect_mssg_back_async(',
        'async def _ask_wait_and_grab_last_assistant_mssg_in_turn(',
       ),
      ),
      ('await asyncio.to_thread(\n            _pick_pool_worker_for_sender',
       'await _resolve_communication_target(body)',
      ),
      ('target = _pick_idle_pool_worker(target_worker_pool)',),
     ),
    )),
    ('detached_lifecycle_check_uses_lite_session_read', (
     ('grep', 'turn_manager.py',
      (('    def _detached_lifecycle_is_active(', '    def _reconcile_inbound_detached('),),
      ('session_manager.get_lite(target_session_id)',), ('session_manager.get(target_session_id)',),
     ),
    )),
    ('bff_projection_source_pre_serializes_off_loop', (
     ('grep', 'main.py',
      (('async def bff_projection_source(', '@app.websocket("/api/bff-runtime/feed")'),),
      ('return _json_bytes_response({"found": False})',
       'return _json_bytes_response({\n            "found": True,',
      ),
      ('return {"found": False}', 'return {\n            "found": True,'),
     ),
    )),
)


_GREP_SOURCE_CACHE: dict[str, str] = {}


def _grep_source(rel: str) -> str:
    text = _GREP_SOURCE_CACHE.get(rel)
    if text is None:
        text = (ROOT / rel).read_text(encoding="utf-8")
        _GREP_SOURCE_CACHE[rel] = text
    return text


def _grep_region(rel: str, steps: tuple) -> str:
    text = _grep_source(rel)
    for start, end in steps:
        lo = text.index(start) if start is not None else 0
        hi = text.index(end, lo) if end is not None else len(text)
        text = text[lo:hi]
    return text


def _grep_check_failures(check: tuple) -> list[str]:
    kind, rel, steps = check[0], check[1], check[2]
    try:
        region = _grep_region(rel, steps)
    except ValueError:
        return [f"{rel}: region marker missing for steps {steps!r}"]
    failures: list[str] = []
    if kind == "grep":
        failures.extend(
            f"{rel}: expected {needle!r}" for needle in check[3] if needle not in region
        )
        failures.extend(
            f"{rel}: forbidden {needle!r}" for needle in check[4] if needle in region
        )
    elif kind == "any_in":
        if not any(needle in region for needle in check[3]):
            failures.append(f"{rel}: none of {check[3]!r} present")
    elif kind == "ordered":
        try:
            positions = []
            for path in check[3]:
                pos = 0
                for needle in path:
                    pos = region.index(needle, pos)
                positions.append(pos)
        except ValueError as exc:
            failures.append(f"{rel}: ordered needle missing: {exc}")
        else:
            if not all(a < b for a, b in zip(positions, positions[1:])):
                failures.append(f"{rel}: order violated for {check[3]!r}")
    elif kind == "count_eq":
        found = region.count(check[3])
        if found != check[4]:
            failures.append(f"{rel}: count({check[3]!r}) == {found}, expected {check[4]}")
    elif kind == "count_ge":
        found = region.count(check[3])
        if found < check[4]:
            failures.append(f"{rel}: count({check[3]!r}) == {found}, expected >= {check[4]}")
    else:
        failures.append(f"unknown check kind {kind!r}")
    return failures


def test_source_grep_regressions() -> None:
    assert len(SOURCE_GREP_CASES) == 190
    executed = 0
    failing: list[str] = []
    report: list[str] = []
    for label, checks in SOURCE_GREP_CASES:
        executed += 1
        entry_failures = [
            failure for check in checks for failure in _grep_check_failures(check)
        ]
        if entry_failures:
            failing.append(label)
            report.append(f"{label}:")
            report.extend(f"  {failure}" for failure in entry_failures)
    assert executed == len(SOURCE_GREP_CASES)
    if failing:
        raise AssertionError(
            f"{len(failing)} of {len(SOURCE_GREP_CASES)} grep cases failed:\n"
            + "\n".join(report)
        )


if __name__ == "__main__":
    import inspect

    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and inspect.isfunction(_fn):
            _fn()
    print("PASS event loop blocking regressions")
