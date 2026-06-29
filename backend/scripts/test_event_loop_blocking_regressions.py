from __future__ import annotations

import tempfile
import sys
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_hook_runner_loads_config_off_loop() -> None:
    source = (ROOT / "hook_runner.py").read_text(encoding="utf-8")
    assert "hooks = await asyncio.to_thread(hook_store.list_hooks)" in source
    assert "hooks = hook_store.list_hooks()" not in source


def test_ownership_projection_uses_dedicated_executor() -> None:
    source = (ROOT / "event_bus_subscribers.py").read_text(encoding="utf-8")
    assert "_OWNERSHIP_PROJECTION_EXECUTOR = ThreadPoolExecutor(" in source
    assert "thread_name_prefix=\"ownership-projection\"" in source
    assert "run_in_executor(\n            _OWNERSHIP_PROJECTION_EXECUTOR" in source
    assert "asyncio.to_thread(\n            session_manager.apply_journal_ownership_resolution" not in source


def test_wire_tailer_gap_fill_reads_journal_off_loop() -> None:
    source = (ROOT / "jsonl_tailer.py").read_text(encoding="utf-8")
    assert "await asyncio.to_thread(\n            event_journal_reader.read_events" in source
    assert "cursor = await asyncio.to_thread(event_journal_reader.cursor" in source
    assert "events, _, _ = event_journal_reader.read_events(" not in source
    assert "cursor = event_journal_reader.cursor(" not in source


def test_jsonl_dispatch_reads_session_lite_off_loop() -> None:
    source = (ROOT / "jsonl_tailer.py").read_text(encoding="utf-8")
    assert "await asyncio.to_thread(session_manager.get_lite, self.app_session_id)" in source
    assert "sess = session_manager.get_lite(self.app_session_id)" not in source


def test_jsonl_fallback_followers_poll_files_off_loop() -> None:
    source = (ROOT / "jsonl_tailer.py").read_text(encoding="utf-8")
    file_start = source.index("class _FileTailFollower:")
    byte_start = source.index("class _AppendOnlyByteFollower:")
    file_source = source[file_start:byte_start]
    byte_end = source.index("class ClaudeJsonlTailer", byte_start)
    byte_source = source[byte_start:byte_end]
    assert "_CURSOR_EXECUTOR" in file_source
    assert "_CURSOR_EXECUTOR" in byte_source
    assert "size = self._path.stat().st_size" not in file_source
    assert "st = self._path.stat()" not in byte_source
    assert "with open(self._path, \"rb\") as f:" not in file_source.split("def _read_from_sync", 1)[0]
    assert "with open(self._path, \"rb\") as f:" not in byte_source.split("def _read_from_sync", 1)[0]


def test_live_provider_stream_mutation_skips_cold_event_hydration() -> None:
    source = (ROOT / "turn_manager.py").read_text(encoding="utf-8")
    start = source.index("async def save_ws_callback(")
    end = source.index("            if event_dict.get(\"type\") in _BRIDGE_EVENT_TYPES:", start)
    callback_source = source[start:end]
    assert "session_manager.message_batch(" in callback_source
    assert "hydrate_events=False" in callback_source
    assert "with session_manager.batch(persist_to):" not in callback_source


def test_subagent_watcher_scans_files_off_loop() -> None:
    source = (ROOT / "jsonl_tailer.py").read_text(encoding="utf-8")
    watch_start = source.index("async def _watch_subagents(")
    watch_end = source.index("def _scan_subagent_files(", watch_start)
    watch_source = source[watch_start:watch_end]
    assert "await asyncio.to_thread(\n                    self._scan_subagent_files" in watch_source
    assert ".exists()" not in watch_source
    assert ".glob(" not in watch_source
    assert ".iterdir(" not in watch_source
    assert ".read_text(" not in watch_source


def test_delegation_locked_reuses_worker_session_snapshot() -> None:
    source = (ROOT / "orchs" / "manager" / "_delegation.py").read_text(encoding="utf-8")
    locked_start = source.index("async def run_delegation_locked(")
    locked_end = source.index("    if machine_completion:", locked_start)
    locked_source = source[locked_start:locked_end]
    assert "worker_session: dict" in locked_source
    assert "worker_session_for_path = session_manager.get(worker_agent_session_id)" not in locked_source
    assert "session_manager.get(worker_agent_session_id)" not in locked_source
    assert "provider_run_config = worker_session.get(\"provider_run_config\")" in locked_source
    assert "capability_contexts = worker_session.get(\"capability_contexts\")" in locked_source
    assert "reasoning_effort = worker_session.get(\"reasoning_effort\")" in locked_source


def test_async_provider_resolution_runs_off_loop() -> None:
    delegation_source = (ROOT / "orchs" / "manager" / "_delegation.py").read_text(encoding="utf-8")
    run_start = delegation_source.index("async def run_delegation(")
    locked_start = delegation_source.index("async def run_delegation_locked(")
    run_source = delegation_source[run_start:locked_start]
    locked_source = delegation_source[locked_start:]
    assert "await asyncio.to_thread(\n                    coordinator.provider_for_session" in run_source
    assert "coordinator.provider_for_session(worker_session_id)" not in run_source
    assert "coordinator.provider_for_session,\n            worker_session_id" in run_source
    assert "coordinator.provider_for_run(worker_agent_session_id, provider_id)" not in locked_source
    assert "coordinator.provider_for_run,\n        worker_agent_session_id" in locked_source

    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    route_start = main_source.index("@app.post(\"/api/internal/headless-generate\")")
    route_end = main_source.index("@app.post(\"/api/internal/headless-run\")", route_start)
    route_source = main_source[route_start:route_end]
    assert "provider = await asyncio.to_thread(coordinator.provider_for_session, session_id)" in route_source


def test_delegation_state_store_calls_run_off_loop() -> None:
    source = (ROOT / "orchs" / "manager" / "_delegation.py").read_text(encoding="utf-8")
    run_start = source.index("async def run_delegation(")
    locked_start = source.index("async def run_delegation_locked(")
    run_source = source[run_start:locked_start]
    locked_source = source[locked_start:]

    assert "caller_session = await asyncio.to_thread(session_manager.get" in run_source
    assert "worker_session = await asyncio.to_thread(session_manager.get" in run_source
    assert "worker_record_result = await asyncio.to_thread(\n        _find_worker_record" in run_source
    assert "session_manager.get(worker_session_id)" not in run_source
    assert "worker_store.get_worker(candidate_cwd, worker_session_id)" not in run_source
    assert "worker_store.remove_worker(candidate_cwd, worker_session_id)" not in run_source

    assert "await asyncio.to_thread(\n                session_fork_store.get_fork_record" in locked_source
    assert "await asyncio.to_thread(session_manager.get, fork_agent_session_id)" in locked_source
    assert "await asyncio.to_thread(session_manager.delete, fork_agent_session_id)" in locked_source
    assert "fork_bc = await asyncio.to_thread(\n                session_manager.create_delegate_fork" in locked_source
    assert "manager_session = await asyncio.to_thread(session_manager.get, app_session_id)" in locked_source
    assert "session_fork_store.get_fork_record(cwd, app_session_id" not in locked_source
    assert "session_manager.get(fork_agent_session_id)" not in locked_source
    assert "session_manager.create_delegate_fork(" not in locked_source


def test_provider_event_rewrite_uses_file_ref_context_not_lite_copy() -> None:
    source = (ROOT / "orchs" / "base.py").read_text(encoding="utf-8")
    start = source.index("def prepare_provider_event_for_journal(")
    end = source.index("    def _apply_worker_event(", start)
    method_source = source[start:end]
    assert "session_manager.get_file_ref_context(app_session_id)" in method_source
    assert "session_manager.get_lite(app_session_id)" not in method_source
    assert "assume_exists_for_node(node_id)" in method_source


def test_jsonl_dispatch_ingests_orphans_off_loop() -> None:
    source = (ROOT / "jsonl_tailer.py").read_text(encoding="utf-8")
    assert "await asyncio.to_thread(\n                    strategy.ingest_orphan" in source
    assert "\n                strategy.ingest_orphan(" not in source


def test_wire_tailer_subscribe_resolves_root_off_loop() -> None:
    source = (ROOT / "orchestrator.py").read_text(encoding="utf-8")
    subscribe_start = source.index("async def _subscribe_to_wire_tailer(")
    subscribe_end = source.index("    def _publish_native_demand(", subscribe_start)
    subscribe_source = source[subscribe_start:subscribe_end]
    assert "root_id = await asyncio.to_thread(\n            session_manager._root_id_for" in subscribe_source
    assert "root_id = session_manager._root_id_for(app_session_id)" not in subscribe_source
    assert "root_id=root_id" in subscribe_source


def test_native_demand_publish_does_not_leak_coroutine_without_loop() -> None:
    source = (ROOT / "orchestrator.py").read_text(encoding="utf-8")
    publish_start = source.index("    def _publish_native_demand(")
    publish_end = source.index("    def _unsubscribe_from_wire_tailer(", publish_start)
    publish_source = source[publish_start:publish_end]
    assert "loop = asyncio.get_running_loop()" in publish_source
    assert "except RuntimeError:\n            return" in publish_source
    assert "asyncio.create_task(\n                bus.publish(" not in publish_source
    assert "loop.create_task(\n            bus.publish(" in publish_source


def test_wire_tailer_unsubscribe_uses_cached_subscriber_root() -> None:
    source = (ROOT / "orchestrator.py").read_text(encoding="utf-8")
    unsubscribe_start = source.index("    def _unsubscribe_from_wire_tailer(")
    unsubscribe_end = source.index("    def _maybe_stop_wire_tailer(", unsubscribe_start)
    unsubscribe_source = source[unsubscribe_start:unsubscribe_end]
    maybe_start = source.index("    def _maybe_stop_wire_tailer(")
    maybe_end = source.index("    async def _await_tailer_stop(", maybe_start)
    maybe_source = source[maybe_start:maybe_end]
    assert "root_ids.add(sub.root_id)" in unsubscribe_source
    assert "session_manager._root_id_for" not in unsubscribe_source
    assert "def _maybe_stop_wire_tailer(self, root_id: str, app_session_id: str)" in maybe_source
    assert "session_manager._root_id_for" not in maybe_source


def test_root_session_write_does_not_resolve_root_id() -> None:
    source = (ROOT / "session_store.py").read_text(encoding="utf-8")
    write_start = source.index("def write_session_full(")
    write_end = source.index("def delete_session(", write_start)
    write_source = source[write_start:write_end]
    assert 'path = _sessions_dir() / f"{root[\'id\']}.json"' in write_source
    assert "_session_path(root[\"id\"])" not in write_source
    assert "_resolve_root_id(root" not in write_source


def test_session_first_prompt_search_uses_summary_index() -> None:
    source = (ROOT / "session_store.py").read_text(encoding="utf-8")
    summary_start = source.index("def _build_summary_for_root(")
    summary_end = source.index("def set_requirement_tags_projection(", summary_start)
    summary_source = source[summary_start:summary_end]
    search_start = source.index("def _metadata_search_scores(")
    search_end = source.index("def grep_session_scores(", search_start)
    search_source = source[search_start:search_end]
    assert '"first_prompt": _first_user_prompt(root)' in summary_source
    assert "rows = _metadata_search_rows()" in search_source
    assert "for sid, _title, first_prompt in rows:" in search_source
    assert "score = first_prompt.count(query_lower)" in search_source
    assert "json.loads(path.read_text" not in search_source
    assert "_migrate_session(" not in search_source


def test_session_content_search_aggregates_in_sqlite() -> None:
    source = (ROOT / "session_search_index.py").read_text(encoding="utf-8")
    search_start = source.index("def search(")
    search_end = source.index("def has_indexed_rows(", search_start)
    search_source = source[search_start:search_end]
    candidate_start = source.index("def _candidate_scores(")
    candidate_end = source.index("def _match_literal(", candidate_start)
    candidate_source = source[candidate_start:candidate_end]
    assert "_candidate_scores(conn, q, limit)" in search_source
    assert "def _inflight_event_for_limit(" in source
    assert "event = _inflight_event_for_limit(q, limit)" in search_source
    assert "COUNT(*) AS score" in candidate_source
    assert "GROUP BY session_id ORDER BY score DESC LIMIT ?" in candidate_source
    assert "SELECT session_id, text" not in candidate_source
    assert "lower().count" not in search_source


def test_bounded_session_content_search_stops_sqlite_scan() -> None:
    source = (ROOT / "session_search_index.py").read_text(encoding="utf-8")
    search_start = source.index("def search(")
    search_end = source.index("def has_indexed_rows(", search_start)
    search_source = source[search_start:search_end]
    fill_start = source.index("def _run_search_cache_fill(")
    fill_end = source.index("def has_indexed_rows(", fill_start)
    fill_source = source[fill_start:fill_end]
    candidate_start = source.index("def _candidate_scores(")
    candidate_end = source.index("def _match_literal(", candidate_start)
    candidate_source = source[candidate_start:candidate_end]
    assert "args=(cache_key, q, limit, max_wait_seconds, event)" in search_source
    assert "deadline = (" in fill_source
    assert "_candidate_scores(conn, query, limit, deadline=deadline)" in fill_source
    assert "conn.set_progress_handler(" in candidate_source
    assert "time.monotonic() >= deadline" in candidate_source
    assert "conn.set_progress_handler(None, 0)" in candidate_source
    assert "interrupted" in candidate_source


def test_session_content_search_uses_readonly_connection_without_writer_lock() -> None:
    source = (ROOT / "session_search_index.py").read_text(encoding="utf-8")
    search_start = source.index("def search(")
    search_end = source.index("def has_indexed_rows(", search_start)
    search_source = source[search_start:search_end]
    connect_start = source.index("def _connect_readonly(")
    connect_end = source.index("def _configure_connection(", connect_start)
    connect_source = source[connect_start:connect_end]
    config_start = source.index("def _configure_connection(")
    config_end = source.index("def _event_text(", config_start)
    config_source = source[config_start:config_end]
    assert "_readonly_conn_local = threading.local()" in source
    assert "def _readonly_connection()" in source
    assert "conn = _readonly_connection()" in search_source
    assert "conn.close()" not in search_source
    assert "with _lock:" not in search_source
    assert "_connect()" not in search_source
    assert "_configure_connection(conn)" in connect_source
    assert "PRAGMA cache_size=-200000" in config_source
    assert "PRAGMA temp_store=MEMORY" in config_source
    assert "PRAGMA mmap_size=268435456" in config_source


def test_session_search_delete_is_queued_projection_work() -> None:
    source = (ROOT / "session_search_index.py").read_text(encoding="utf-8")
    assert "_writer_conn" in source
    assert "def _writer_connection()" in source
    delete_start = source.index("def delete_session(")
    delete_end = source.index("def search(", delete_start)
    delete_source = source[delete_start:delete_end]
    worker_start = source.index("def _worker_main(")
    worker_end = source.index("def _apply_rows(", worker_start)
    worker_source = source[worker_start:worker_end]
    apply_start = source.index("def _apply_rows(")
    apply_end = source.index("def _drain_pending(", apply_start)
    apply_source = source[apply_start:apply_end]
    assert "_queue.put((session_id, None))" in delete_source
    assert "with _lock:" not in delete_source
    assert "conn = _writer_connection()" in apply_source
    assert "conn.close()" not in apply_source
    assert "conn = _connect()" not in apply_source
    assert "DELETE FROM session_event_fts" in apply_source


def test_event_journal_rejects_late_writes_after_close() -> None:
    source = (ROOT / "event_journal.py").read_text(encoding="utf-8")
    assert "self._closed = False" in source
    assert "self._closed = True" in source
    assert 'raise EventJournalWriteError("event journal writer is closed")' in source


def test_publish_event_default_path_skips_temp_ack_subscribers() -> None:
    source = (ROOT / "event_journal.py").read_text(encoding="utf-8")
    start = source.index("async def publish_event(")
    end = source.index("def publish_event_sync(", start)
    publish_source = source[start:end]
    default_start = publish_source.index("if bus_instance is bus:")
    fallback_start = publish_source.index("loop = asyncio.get_running_loop()")
    default_source = publish_source[default_start:fallback_start]
    assert "event_journal_writer.submit_event_async(Event(" in default_source
    assert "bus_instance.subscribe(" not in default_source
    assert "event_journal_ack_" not in default_source


def test_broadcast_session_journal_write_runs_off_loop() -> None:
    source = (ROOT / "orchestrator.py").read_text(encoding="utf-8")
    start = source.index("async def broadcast_session(")
    end = source.index("async def broadcast_global(", start)
    broadcast_source = source[start:end]
    assert "await asyncio.to_thread(" in broadcast_source
    assert "self._broadcast_session_sync" in broadcast_source
    assert "await publish_event(" not in broadcast_source
    assert "publish_event_sync(" in broadcast_source


def test_provider_complete_watcher_filesystem_poll_runs_off_loop() -> None:
    for filename in ("provider_claude.py", "provider_codex.py", "provider_gemini.py"):
        source = (ROOT / filename).read_text(encoding="utf-8")
        start = source.index("async def _watch_complete(")
        if filename == "provider_claude.py":
            end = source.index("async def _watch_linger_exit(", start)
        elif filename == "provider_codex.py":
            end = source.index("async def _ensure_child_tailer(", start)
        else:
            end = source.index("# ------------------------------------------------------------------\n    # _emit_complete_from_file", start)
        watcher_source = source[start:end]
        assert "await asyncio.to_thread(complete_path.exists)" in watcher_source
        assert "complete_path.exists()" not in watcher_source


def test_codex_cursor_state_write_is_coalesced_off_loop() -> None:
    source = (ROOT / "provider_codex.py").read_text(encoding="utf-8")
    root_cursor_start = source.index("        def _on_cursor(")
    root_cursor_end = source.index("        rs.tailer = CodexRolloutTailer(", root_cursor_start)
    root_cursor_source = source[root_cursor_start:root_cursor_end]
    assert "self._schedule_backend_state_flush(_rs)" in root_cursor_source
    assert "self._write_backend_state(_rs)" not in root_cursor_source

    child_cursor_start = source.index("        def _on_child_cursor(")
    child_cursor_end = source.index("        tailer = CodexRolloutTailer(", child_cursor_start)
    child_cursor_source = source[child_cursor_start:child_cursor_end]
    assert "self._schedule_backend_state_flush(_rs)" in child_cursor_source
    assert "self._write_backend_state(_rs)" not in child_cursor_source

    flush_start = source.index("    async def _flush_backend_state_async(")
    flush_end = source.index("    def attach_recovered_run(", flush_start)
    flush_source = source[flush_start:flush_end]
    assert "await asyncio.to_thread(self._write_backend_state, rs)" in flush_source


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


def test_internal_workers_list_runs_projection_off_loop() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    route_start = source.index("async def internal_list_workers_for_cwd(")
    route_end = source.index("def _worker_pool_projection(", route_start)
    route_source = source[route_start:route_end]
    assert "return await asyncio.to_thread(_internal_list_workers_for_cwd_sync, cwd)" in route_source
    assert "compute_jsonl_path(" not in route_source
    assert "count_jsonl_lines(" not in route_source
    assert "session_manager.get_lite(" not in route_source


def test_message_delta_replay_skips_full_snapshot_rebuild() -> None:
    source = (ROOT / "session_manager.py").read_text(encoding="utf-8")
    start = source.index("def get_messages_since(")
    end = source.index("def _get_cached_snapshot(", start)
    method_source = source[start:end]
    delta_start = method_source.index("if since_seq > 0:")
    snapshot_start = method_source.index("snapshot = self._get_cached_snapshot(")
    delta_source = method_source[delta_start:snapshot_start]
    assert "_compute_messages_window(" in delta_source
    assert "_get_cached_snapshot(" not in delta_source


def test_connected_session_fallback_sorts_only_requested_page() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "def _filter_sort_page_for_list(" in source
    route_start = source.index("async def get_sessions(")
    route_end = source.index("@app.post(\"/api/sessions/search-content\")", route_start)
    route_source = source[route_start:route_end]
    fallback_start = route_source.index("if can_page_remote_local_order:")
    fallback_source = route_source[fallback_start:route_source.index("elif _can_preserve_summary_order", fallback_start)]
    assert "_filter_sort_page_for_list" in fallback_source
    assert "_filter_sort_sessions_for_list" not in fallback_source


def test_message_cache_hydration_has_substep_perf_metrics() -> None:
    source = (ROOT / "event_journal.py").read_text(encoding="utf-8")
    start = source.index("def _ensure_message_cache(")
    end = source.index("def read_message_frontend_events(", start)
    cache_source = source[start:end]
    assert "event_journal.message_cache.summaries" in cache_source
    assert "event_journal.message_cache.resolutions" in cache_source
    assert "event_journal.message_cache.read_full" in cache_source
    assert "event_journal.message_cache.read_grow" in cache_source


def test_node_link_runtime_readiness_uses_ttl_cache() -> None:
    source = (ROOT / "node_link.py").read_text(encoding="utf-8")
    assert "_MACHINE_NODES_READY_CACHE_TTL_S" in source
    start = source.index("def _machine_nodes_not_ready_reason(")
    end = source.index("def set_registration_listener(", start)
    readiness_source = source[start:end]
    assert "time.monotonic()" in readiness_source
    assert "_machine_nodes_ready_cache" in readiness_source
    assert "runtime_not_ready_message(" in readiness_source


def test_connected_session_list_pages_virtual_candidates() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    connected_start = source.index("    if connected:")
    connected_source = source[connected_start:source.index("@app.post(\"/api/sessions/search-content\")", connected_start)]
    virtual_start = connected_source.index("if may_include_virtual:")
    virtual_source = connected_source[virtual_start:connected_source.index("try:", virtual_start)]
    assert "if can_page_remote_local_order:" in virtual_source
    assert "virtual_session_store.list_recent" in virtual_source
    assert "max(offset + limit, 1)" in virtual_source
    assert "virtual_session_store.list_all" in virtual_source


def test_connected_session_list_skips_full_sort_without_remote_merge() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    start = source.index("async def get_sessions(")
    end = source.index("@app.post(\"/api/sessions/search-content\")", start)
    route_source = source[start:end]
    assert "appended_remote_sessions = False" in route_source
    fast_path = (
        "can_page_remote_local_order\n"
        "        and not appended_virtual_sessions\n"
        "        and not appended_remote_sessions\n"
        "        and local_total is not None"
    )
    assert fast_path in route_source
    assert route_source.index(fast_path) < route_source.index("with perf.timed(\"sessions.list.filter_sort\")")


def test_delegation_status_writes_run_off_loop() -> None:
    store_source = (ROOT / "delegation_status_store.py").read_text(encoding="utf-8")
    assert "async def write_status_async(" in store_source
    assert "await asyncio.to_thread(write_status" in store_source
    source = (ROOT / "orchs" / "manager" / "_delegation.py").read_text(encoding="utf-8")
    start = source.index("async def run_delegation(")
    run_source = source[start:]
    assert "await delegation_status_store.write_status_async(" in run_source
    assert "delegation_status_store.write_status(" not in run_source


def test_team_ask_status_writes_run_off_loop() -> None:
    store_source = (ROOT / "ask_status_store.py").read_text(encoding="utf-8")
    assert "async def write_status_async(" in store_source
    assert "await asyncio.to_thread(write_status" in store_source
    source = (ROOT / "orchestrator.py").read_text(encoding="utf-8")
    start = source.index("async def ask_team_message(")
    end = source.index("    def _team_message_turn_response(", start)
    ask_source = source[start:end]
    assert "await ask_status_store.write_status_async(" in ask_source
    assert "ask_status_store.write_status(" not in ask_source


def test_team_message_context_uses_lite_session_read() -> None:
    source = (ROOT / "team_messaging.py").read_text(encoding="utf-8")
    start = source.index("def _target_team_context(")
    end = source.index("def format_team_message_prompt(", start)
    context_source = source[start:end]
    assert "session_manager.get_lite(target_session_id)" in context_source
    assert "session_manager.get(target_session_id)" not in context_source


def test_team_message_validation_uses_lite_session_read() -> None:
    source = (ROOT / "team_messaging.py").read_text(encoding="utf-8")
    start = source.index("def validate_message_route(")
    end = source.index("def build_message_metadata(", start)
    validation_source = source[start:end]
    assert "session_manager.get_lite(sender_session_id)" in validation_source
    assert "session_manager.get_lite(target_session_id)" in validation_source
    assert "session_manager.exists(" not in validation_source
    assert "session_manager.get(" not in validation_source


def test_session_exists_uses_index_without_cold_root_load() -> None:
    source = (ROOT / "session_manager.py").read_text(encoding="utf-8")
    start = source.index("    def exists(self, sid: str) -> bool:")
    end = source.index("    def get_field(", start)
    exists_source = source[start:end]
    assert "session_store._resolve_root_id(sid)" in exists_source
    assert "self._load_root(" not in exists_source
    assert exists_source.count("session_store._find_in_tree(root, sid)") == 1


def test_session_detail_reuses_migrated_root_cache() -> None:
    source = (ROOT / "session_store.py").read_text(encoding="utf-8")
    assert "_migrated_root_cache" in source
    assert "def _cached_migrated_root(" in source
    helper_start = source.index("def _cached_migrated_root(")
    helper_end = source.index("def read_node_kind_record(", helper_start)
    helper_source = source[helper_start:helper_end]
    assert "cache_key = (root_id, file_signature)" in helper_source
    assert "return copy.deepcopy(cached)" in helper_source
    detail_start = source.index("def get_root_tree(")
    detail_end = source.index("def _strip_volatile_from_tree(", detail_start)
    detail_source = source[detail_start:detail_end]
    assert "_cached_migrated_root(root_id, file_signature, root)" in detail_source
    assert detail_source.index("_cached_migrated_root(") < detail_source.index("_overlay_drafts(")


def test_extension_plain_load_is_read_only() -> None:
    source = (ROOT / "extension_store.py").read_text(encoding="utf-8")
    load_start = source.index("def _load()")
    load_end = source.index("def _save(", load_start)
    load_source = source[load_start:load_end]
    assert "_read_store_unlocked()" in load_source
    assert "_load_with_changes()" not in load_source


def test_recovery_dispatch_skips_reconciled_runs_before_owner_read() -> None:
    source = (ROOT / "provider.py").read_text(encoding="utf-8")
    start = source.index("def recover_all_in_flight(")
    end = len(source)
    recover_source = source[start:end]
    marker_idx = recover_source.index('marker_path = child / "reconciled.marker"')
    backend_state_idx = recover_source.index('bs_path = child / "backend_state.json"')
    assert marker_idx < backend_state_idx
    assert "marker_data_matches_current(" in recover_source[marker_idx:backend_state_idx]
    assert "marker_matches_current(" not in recover_source[marker_idx:backend_state_idx]


def test_filtered_provider_recovery_does_not_rescan_all_runs() -> None:
    runs_source = (ROOT / "runs_dir.py").read_text(encoding="utf-8")
    assert "def iter_run_dirs(run_id_filter: Optional[set[str]] = None)" in runs_source
    assert "for run_id in run_id_filter:" in runs_source

    for filename in (
        "provider_claude.py",
        "provider_codex.py",
        "provider_gemini.py",
        "provider_openai.py",
    ):
        source = (ROOT / filename).read_text(encoding="utf-8")
        start = source.index("    def recover_in_flight(")
        end = source.index("    # ------------------------------------------------------------------", start)
        recover_source = source[start:end]
        assert "iter_run_dirs(run_id_filter)" in recover_source
        assert "child.name not in run_id_filter" not in recover_source


def test_provider_prune_uses_shared_scandir_helper() -> None:
    runs_source = (ROOT / "runs_dir.py").read_text(encoding="utf-8")
    assert "def prune_old_completed_runs(max_age_days: int = 7) -> int" in runs_source
    assert "with os.scandir(root) as entries:" in runs_source

    for filename in (
        "provider_claude.py",
        "provider_codex.py",
        "provider_gemini.py",
        "provider_openai.py",
    ):
        source = (ROOT / filename).read_text(encoding="utf-8")
        start = source.index("    def prune_old_runs(")
        end = source.index("    # ------------------------------------------------------------------", start + 1)
        prune_source = source[start:end]
        assert "prune_old_completed_runs(max_age_days)" in prune_source
        assert "_runs_root().iterdir()" not in prune_source


def test_session_fork_index_refresh_is_root_scoped() -> None:
    source = (ROOT / "session_store.py").read_text(encoding="utf-8")
    start = source.index("def _index_tree(")
    end = source.index("def _index_set(", start)
    index_source = source[start:end]
    assert "_root_forks.get(rid" in index_source
    assert "_fork_index.items()" not in index_source
    assert "_reconcile_loaded_store" not in index_source
    assert "_root_index_signatures.get(rid) == file_signature" in index_source
    assert index_source.index("_root_index_signatures.get(rid)") < index_source.index("for fork in _walk_forks(root)")

    get_start = source.index("def get_root_tree(")
    get_end = source.index("def _strip_volatile_from_tree(", get_start)
    get_source = source[get_start:get_end]
    assert "file_signature = _session_file_signature(path)" in get_source
    assert "_index_tree(root, file_signature=file_signature)" in get_source
    assert "if session_id != root_id:" in get_source
    assert get_source.index("if session_id != root_id:") < get_source.index("_index_tree(root, file_signature=file_signature)")


def test_session_organization_reads_are_cached() -> None:
    source = (ROOT / "session_organization_store.py").read_text(encoding="utf-8")
    assert "_cache_signature" in source
    assert "_cache_data" in source
    assert "def _load_shared()" in source
    load_start = source.index("def _load()")
    load_end = source.index("def _save(", load_start)
    load_source = source[load_start:load_end]
    assert "return copy.deepcopy(data)" in load_source
    shared_start = source.index("def _load_shared()")
    shared_end = source.index("def _load()", shared_start)
    shared_source = source[shared_start:shared_end]
    assert "_cache_signature == signature" in shared_source
    assert "return _cache_data" in shared_source
    enrich_start = source.index("def enrich_session_summaries(")
    enrich_end = source.index("def create_folder(", enrich_start)
    enrich_source = source[enrich_start:enrich_end]
    assert "data = _load_shared()" in enrich_source
    assert "_assignment(" not in enrich_source


def test_jsonl_cursor_persistence_uses_dedicated_executor() -> None:
    source = (ROOT / "jsonl_tailer.py").read_text(encoding="utf-8")
    assert "_CURSOR_EXECUTOR = ThreadPoolExecutor(" in source
    assert "thread_name_prefix=\"jsonl-cursor\"" in source
    assert "await loop.run_in_executor(\n                _CURSOR_EXECUTOR" in source
    assert "self.on_cursor_advance(self.processed_offset)" not in source


def test_event_ingester_indexes_search_outside_root_lock() -> None:
    source = (ROOT / "event_ingester.py").read_text(encoding="utf-8")
    assert "session_search_index" not in source


def test_private_extension_reconcile_skips_current_smoked_install() -> None:
    source = (ROOT / "extension_store.py").read_text(encoding="utf-8")
    private_start = source.index("def _ensure_private_extensions(")
    private_end = source.index("def is_builtin_feature_enabled(", private_start)
    private_source = source[private_start:private_end]
    assert 'source.get("type") == "better_agent_local"' in private_source
    assert 'source.get("commit_sha") == commit_sha' in private_source
    assert 'not source.get("error")' in private_source
    assert "_record_has_required_runtime_paths(record)" in private_source
    assert "_record_runtime_ready(record)" not in private_source
    skip_pos = private_source.index("continue", private_source.index("_record_has_required_runtime_paths(record)"))
    install_pos = private_source.index("installed = _install_private_package_snapshot", skip_pos)
    assert skip_pos < install_pos


def test_frontend_entrypoints_do_not_run_smoke_subprocesses() -> None:
    source = (ROOT / "extension_store.py").read_text(encoding="utf-8")
    ready_start = source.index("def _record_runtime_ready(")
    ready_end = source.index("def _record_has_required_runtime_paths(", ready_start)
    ready_source = source[ready_start:ready_end]
    frontend_start = source.index("def frontend_entrypoints(")
    frontend_end = source.index("def resolve_frontend_asset(", frontend_start)
    frontend_source = source[frontend_start:frontend_end]
    assert "_record_smoke_test_current(record)" in ready_source
    assert "_record_smoke_test_passes(record)" not in ready_source
    assert "_run_extension_smoke_test(" not in ready_source
    assert "_run_python_module_smoke(" not in ready_source
    assert "_record_runtime_ready(record)" in frontend_source
    assert "_run_extension_smoke_test(" not in frontend_source


def test_extension_list_uses_projection_cache() -> None:
    source = (ROOT / "extension_store.py").read_text(encoding="utf-8")
    start = source.index("def list_extensions(")
    end = source.index("def _active_records(", start)
    list_source = source[start:end]
    assert '_projection_cache_get("list_extensions"' in list_source
    assert '_projection_cache_put(\n        "list_extensions",' in list_source
    assert "return list_extensions(include_hidden=include_hidden), False" in list_source


def test_extension_projection_routes_cache_json_bytes() -> None:
    source = (ROOT / "extension_api.py").read_text(encoding="utf-8")
    assert "_projection_response_cache" in source
    assert "def _projection_response_cache_get(" in source
    assert "def _projection_response_cache_put(" in source
    assert "def _cached_json_projection_response(" in source
    assert "json.dumps(" in source
    assert "Response(content=content, media_type=\"application/json\")" in source

    frontend_start = source.index("async def get_frontend_entrypoints(")
    frontend_end = source.index("@router.get(\"/ui-hooks\")", frontend_start)
    frontend_source = source[frontend_start:frontend_end]
    assert "_cached_json_projection_response(" in frontend_source
    assert "extension_store.frontend_entrypoints_cache_key()" in frontend_source
    assert "extension_store.frontend_entrypoints()" in frontend_source

    hooks_start = source.index("async def get_ui_hooks(")
    hooks_end = source.index("@router.get(\"/{extension_id}/frontend/{asset_path:path}\")", hooks_start)
    hooks_source = source[hooks_start:hooks_end]
    assert "_cached_json_projection_response(" in hooks_source
    assert "extension_store.ui_hooks_cache_key()" in hooks_source
    assert "extension_store.ui_hooks()" in hooks_source


def test_startup_reenqueue_reads_sessions_off_loop() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "await asyncio.to_thread(\n                    session_manager.get_lite" in source


def test_queue_projection_scans_user_messages_once() -> None:
    source = (ROOT / "session_queue_projection.py").read_text(encoding="utf-8")
    assert "def _user_message_projection(" in source
    assert "def _user_message_keys(" not in source
    assert "def _user_messages(" not in source
    project_start = source.index("def project_session(")
    project_end = source.index("def upsert_from_session(", project_start)
    project_source = source[project_start:project_end]
    assert "user_projection = _user_message_projection(" in project_source
    assert "**user_projection" in project_source


def test_queue_projection_skips_unchanged_disk_write() -> None:
    source = (ROOT / "session_queue_projection.py").read_text(encoding="utf-8")
    start = source.index("def upsert_from_session(")
    end = source.index("def get(", start)
    upsert_source = source[start:end]
    assert 'if _records.get(record["id"]) == record:' in upsert_source
    assert upsert_source.index('if _records.get(record["id"]) == record:') < upsert_source.index("_write_record_locked(record)")


def test_startup_does_not_warm_unread_by_hydrating_sessions() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "startup-unread-warm" not in source
    assert "_warm_unread_counts" not in source


def test_startup_defers_requirement_and_project_match_warmers() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    startup_start = source.index("async def on_startup()")
    startup_end = source.index("async def on_shutdown()", startup_start)
    startup_source = source[startup_start:startup_end]
    assert "startup-requirements-prewarm" not in startup_source
    assert "run_requirements_prewarm" not in startup_source
    assert "project-match-warm" not in startup_source
    assert "_ensure_project_match_warm_task()" in source


def test_startup_defers_shortcut_http_prewarm() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    startup_start = source.index("async def on_startup()")
    startup_end = source.index("async def on_shutdown()", startup_start)
    startup_source = source[startup_start:startup_end]
    assert "shortcut_picker.prewarm_http_stack" in startup_source
    assert "await asyncio.to_thread(shortcut_picker.prewarm_http_stack)" not in startup_source
    assert "_fire_and_forget(asyncio.to_thread(shortcut_picker.prewarm_http_stack))" in startup_source


def test_sidebar_organization_enrichment_stays_in_summary_index() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    local_start = source.index("def _local_session_summaries_for_sidebar()")
    local_end = source.index("def _root_session_file_path(", local_start)
    local_source = source[local_start:local_end]
    assert "enrich_session_summaries(" not in local_source
    assert "enrich_session_summary(" not in local_source
    assert "session_store._ensure_summary_index(blocking=True)" not in local_source

    store_source = (ROOT / "session_store.py").read_text(encoding="utf-8")
    build_start = store_source.index("def _build_summary_for_root(")
    build_end = store_source.index("def set_requirement_tags_projection(", build_start)
    build_source = store_source[build_start:build_end]
    assert "enrich_session_summary(summary)" in build_source
    assert "enrich_session_summary_from_projection(" in build_source

    org_source = (ROOT / "session_organization_store.py").read_text(encoding="utf-8")
    enrich_start = org_source.index("def enrich_session_summary(")
    enrich_end = org_source.index("def enrich_session_summaries(", enrich_start)
    enrich_source = org_source[enrich_start:enrich_end]
    assert "_load_shared()" in enrich_source
    assert "organization_for_session(" not in enrich_source
    assert "_load()" not in enrich_source


def test_session_organization_facets_are_version_cached() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "_session_org_facets_cache" in source
    start = source.index("def _session_organization_snapshot_with_facets(")
    end = source.index("@app.get(\"/api/session-organization\")", start)
    facets_source = source[start:end]
    assert "session_organization_store.version_token()" in facets_source
    assert "session_store.summary_version()" in facets_source
    assert "_session_org_facets_cache.get(cache_key)" in facets_source
    assert "_local_session_summaries_for_sidebar()" in facets_source


def test_session_organization_query_builds_tag_sets_only_for_tag_filter() -> None:
    source = (ROOT / "session_organization_store.py").read_text(encoding="utf-8")
    start = source.index("def query_sessions(")
    end = len(source)
    query_source = source[start:end]
    tag_branch = query_source.index("if tag_set:")
    session_tags = query_source.index("session_tags = {")
    assert tag_branch < session_tags


def test_sidebar_decoration_uses_bulk_cached_state() -> None:
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "def _sidebar_state_snapshot()" in main_source
    payload_start = main_source.index("def _sidebar_session_payload(")
    payload_end = main_source.index("def _sidebar_state_snapshot(", payload_start)
    payload_source = main_source[payload_start:payload_end]
    assert 'if key != "first_prompt"' in payload_source
    assert 'payload.pop("first_prompt", None)' not in payload_source
    start = main_source.index("def _decorate_local_sidebar_sessions(")
    end = main_source.index("def _local_sessions_for_sidebar(", start)
    decorate_source = main_source[start:end]
    assert "_sidebar_state_snapshot()" in decorate_source
    assert "is_running_cached(" not in decorate_source
    assert "monitoring_state_cached(" not in decorate_source
    helper_start = main_source.index("def _build_local_sessions_page_for_list(")
    helper_end = main_source.index("async def _sidebar_search_scores(", helper_start)
    helper_source = main_source[helper_start:helper_end]
    assert "state_snapshot = _sidebar_state_snapshot() if status_sort else None" in helper_source
    assert "_decorate_local_sidebar_sessions(out[offset:end], state_snapshot)" in helper_source

    turn_source = (ROOT / "turn_manager.py").read_text(encoding="utf-8")
    assert "def cached_state_snapshot(" in turn_source


def test_session_discovery_reads_mode_without_deepcopy() -> None:
    source = (ROOT / "turn_manager.py").read_text(encoding="utf-8")
    start = source.index('if event.type == "session_discovered":')
    end = source.index('if event.type in ("complete", "error"):', start)
    discovery_source = source[start:end]
    assert 'session_manager.get_field(' in discovery_source
    assert '"orchestration_mode"' in discovery_source
    assert 'session_manager.get(' not in discovery_source


def test_project_aggregates_use_bulk_cached_state() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    start = source.index("def _project_aggregates(")
    end = source.index("def _invalidate_project_aggregates(", start)
    aggregate_source = source[start:end]
    assert "cached_state_snapshot()" in aggregate_source
    assert "unread_counts_snapshot()" in aggregate_source
    assert "is_running_cached(" not in aggregate_source
    assert "peek_unread_count(" not in aggregate_source


def test_sidebar_file_paths_use_cached_sessions_dir() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "def _root_sessions_dir_path(" in source
    start = source.index("def _decorate_local_sidebar_sessions(")
    end = source.index("def _local_sessions_for_sidebar(", start)
    decorate_source = source[start:end]
    assert "sessions_dir = _root_sessions_dir_path()" in decorate_source
    assert '"file_path": f"{sessions_dir}/{sid}.json"' in decorate_source
    assert "ba_home()" not in decorate_source
    assert "_root_session_file_path(sid)" not in decorate_source


def test_session_list_uses_sorted_summary_cache() -> None:
    source = (ROOT / "session_store.py").read_text(encoding="utf-8")
    assert "_summary_sorted_cache_version" in source
    assert "_summary_sorted_id_cache" in source
    assert "_summary_sorted_id_caches" in source
    assert "_summary_order_version" in source
    assert "_summary_projected_cache_version" not in source
    assert "_summary_projected_cache" not in source
    assert "_replace_summary_projection_field" in source
    assert "def ordered_session_summary_ids(" in source
    assert "def _summary_order_changed(" in source
    assert '"last_user_prompt_at"' in source
    start = source.index("def list_sessions()")
    end = source.index("def iter_all_sessions()", start)
    list_source = source[start:end]
    assert "_summary_sorted_cache_version != _summary_order_version" in list_source
    assert "_summary_sorted_id_cache = [" in list_source
    assert "sorted(\n                    _summary_index.values()" in list_source
    assert "_summary_index[sid]" in list_source
    assert "_requirement_tags_snapshot()" not in list_source
    assert "_markers_snapshot()" not in list_source


def test_session_list_pages_last_user_prompt_order_before_full_sort() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    helper_start = source.index("def _local_session_page_for_sidebar_preserving_order(")
    helper_end = source.index("def _root_session_file_path(", helper_start)
    helper_source = source[helper_start:helper_end]
    assert "session_manager.ordered_summary_ids(sort_by)" in helper_source
    assert "_filter_sort_sessions_for_list(" not in helper_source
    assert "sessions.list.local.ordered_filter" in helper_source

    route_start = source.index("def _build_local_sessions_page_for_list(")
    route_end = source.index("@app.get(\"/api/sessions\")", route_start)
    route_source = source[route_start:route_end]
    gate_start = source.index("def _can_page_local_summary_order(")
    gate_end = source.index("def _build_local_sessions_page_for_list(", gate_start)
    gate_source = source[gate_start:gate_end]
    assert 'sort_by in {"updated_at", "last_user_prompt_at"}' in gate_source
    assert 'sort_by == "last_user_prompt_at"' in route_source
    assert "sessions.list.local_order_page" in route_source
    assert "sessions.list.virtual_count" in route_source
    assert "if default_virtual_page:" in route_source
    assert "limit=max(offset + limit, 1)" in route_source
    assert route_source.index("sessions.list.local_order_page") < route_source.index(
        'sessions.list.local"):'
    )
    connected_start = source.index("async def get_sessions(")
    connected_end = source.index("@app.post(\"/api/sessions/search-content\")", connected_start)
    connected_source = source[connected_start:connected_end]
    assert "sessions.list.remote.local_order_candidates" in connected_source
    assert "can_page_remote_local_order" in connected_source
    assert connected_source.index("sessions.list.remote.local_order_candidates") < connected_source.index(
        'sessions.list.local"):'
    )


def test_session_list_skips_impossible_virtual_filters() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    helper_start = source.index("def _session_filters_may_include_virtual(")
    helper_end = source.index("def _build_local_sessions_page_for_list(", helper_start)
    helper_source = source[helper_start:helper_end]
    assert "if file_edit_mode is True:" in helper_source
    assert "if folder_ids or tag_ids:" in helper_source
    assert 'if modes and "virtual" not in modes:' in helper_source
    assert 'if sources and not ({"extension", "system"} & sources):' in helper_source

    local_start = source.index("def _build_local_sessions_page_for_list(")
    local_end = source.index("@app.get(\"/api/sessions\")", local_start)
    local_source = source[local_start:local_end]
    assert "_session_filters_may_include_virtual(" in local_source
    assert "virtual_session_store.list_recent(" in local_source
    assert "max(offset + limit, 1)" in local_source
    assert 'perf.record("sessions.list.virtual.skipped", 1.0)' in local_source

    route_start = source.index("async def get_sessions(")
    route_end = source.index("@app.post(\"/api/sessions/search-content\")", route_start)
    route_source = source[route_start:route_end]
    assert "_session_filters_may_include_virtual(" in route_source
    assert "virtual_session_store.list_all" in route_source
    assert 'perf.record("sessions.list.virtual.skipped", 1.0)' in route_source


def test_session_list_preserves_summary_order_when_no_virtual_rows() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    helper_start = source.index("def _can_preserve_summary_order(")
    helper_end = source.index("def _session_filters_may_include_virtual(", helper_start)
    helper_source = source[helper_start:helper_end]
    assert "appended_virtual_sessions: bool" in helper_source
    assert "and not appended_virtual_sessions" in helper_source
    assert "virtual_sessions: list[dict]" not in helper_source

    local_start = source.index("def _build_local_sessions_page_for_list(")
    local_end = source.index("@app.get(\"/api/sessions\")", local_start)
    local_source = source[local_start:local_end]
    assert "appended_virtual_sessions = False" in local_source
    assert "virtual_sidebar_sessions = [" in local_source
    assert "_can_page_default_updated_at_with_virtual(" in local_source
    assert "_merge_updated_at_page(" in local_source
    assert "if virtual_sidebar_sessions:" in local_source
    assert "appended_virtual_sessions = True" in local_source
    assert "appended_virtual_sessions=appended_virtual_sessions" in local_source
    assert "_filter_page_for_list_preserving_order(" in local_source
    assert "_decorate_local_sidebar_sessions(page_source, state_snapshot)" in local_source
    page_start = source.index("def _filter_page_for_list_preserving_order(")
    page_end = source.index("def _can_preserve_summary_order(", page_start)
    page_source = source[page_start:page_end]
    assert "page.append(session)" in page_source
    assert "return page, total" in page_source

    route_start = source.index("async def get_sessions(")
    route_end = source.index("@app.post(\"/api/sessions/search-content\")", route_start)
    route_source = source[route_start:route_end]
    assert "appended_virtual_sessions = False" in route_source
    assert "virtual_sidebar_sessions = [" in route_source
    assert "if virtual_sidebar_sessions:" in route_source
    assert "appended_virtual_sessions = True" in route_source
    assert "_filter_sessions_for_list_preserving_order" in route_source


def test_session_tag_filter_uses_summary_projection() -> None:
    store_source = (ROOT / "session_store.py").read_text(encoding="utf-8")
    assert '"tag_filter_ids": _tag_filter_ids(' in store_source
    assert 'summary["tag_filter_ids"] = _tag_filter_ids(' in store_source
    assert '"tag_filter_ids": tag_filter_ids' in store_source
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    match_start = main_source.index("def _session_matches_list_filters(")
    match_end = main_source.index("def _session_filtered_sort_key(", match_start)
    match_source = main_source[match_start:match_end]
    assert 'filter_ids = session.get("tag_filter_ids")' in match_source
    assert "_session_tag_filter_ids(session)" in match_source
    assert "manual_tags = {" not in match_source
    assert "requirement_tags = {" not in match_source


def test_session_timestamp_sort_value_is_cached() -> None:
    source = (ROOT / "session_store.py").read_text(encoding="utf-8")
    assert "from functools import lru_cache" in source
    assert "@lru_cache(maxsize=4096)\ndef _timestamp_sort_value_str" in source
    start = source.index("def timestamp_sort_value(")
    end = source.index("def _newer_timestamp(", start)
    helper_source = source[start:end]
    assert "return _timestamp_sort_value_str(value)" in helper_source


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
    internal_end = source.index("@app.post(\"/api/internal/goal/set\")", internal_start)
    internal_source = source[internal_start:internal_end]
    assert "public_req = await asyncio.to_thread(" in internal_source
    assert "user_input_store.create_request" in internal_source
    assert "user_input_store.create_request(" not in internal_source.replace(
        "user_input_store.create_request,\n",
        "",
    )


def test_shortcut_picker_wait_budget_is_small() -> None:
    source = (ROOT / "shortcut_picker.py").read_text(encoding="utf-8")
    assert "_PICK_WAIT_TIMEOUT_SECS = 0.25" in source
    pick_start = source.index("async def pick_shortcuts(")
    pick_source = source[pick_start:]
    timeout_start = pick_source.index("except asyncio.TimeoutError:")
    exception_start = pick_source.index("except Exception:")
    timeout_source = pick_source[timeout_start:exception_start]
    assert "asyncio.wait_for(" in pick_source
    assert "await asyncio.to_thread(\n            _shortcut_picker_inputs," in pick_source
    assert "fallback_shortcuts = list(all_shortcuts)" in pick_source
    assert "if fallback_shortcuts is not None:" in timeout_source
    assert timeout_source.index("if fallback_shortcuts is not None:") < timeout_source.index(
        "await asyncio.to_thread(user_prefs.get_shortcut_responses)"
    )
    assert "return await asyncio.shield(_cached_pick(key, _pick_uncached))" in pick_source
    assert "user_prefs.get_shortcut_responses()" not in pick_source
    assert "config_store.get_default_provider()" not in pick_source
    assert "return all_shortcuts" in pick_source


def test_stubbed_tree_build_does_not_search_tree_per_node() -> None:
    source = (ROOT / "session_manager.py").read_text(encoding="utf-8")
    start = source.index("def _build_stubbed_tree(")
    end = source.index("def _compute_messages_snapshot(", start)
    build_source = source[start:end]
    assert "session_store._find_in_tree(root, node_sid)" not in build_source
    assert "node_sid, rid, node_src" in build_source


def test_tree_stub_cache_key_reads_render_seq_once() -> None:
    source = (ROOT / "session_manager.py").read_text(encoding="utf-8")
    start = source.index("def _tree_stub_cache_key(")
    end = source.index("def _build_stubbed_tree(", start)
    key_source = source[start:end]
    assert "render_seq_by_sid = event_ingester.render_seq_by_sid(rid)" in key_source
    assert "render_seq_for_sid(" not in key_source


def test_session_event_meta_uses_combined_ingester_read() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    roots_start = source.index("def _session_event_meta_roots_for_page(")
    roots_end = source.index("async def _warm_session_event_meta_roots(", roots_start)
    roots_source = source[roots_start:roots_end]
    assert "_session_event_file_fingerprint(root_id) == (0, 0)" not in roots_source
    helper_start = source.index("def _session_event_meta(")
    helper_end = source.index("def _session_event_meta_cache_fresh(", helper_start)
    helper_source = source[helper_start:helper_end]
    assert "event_ingester.session_event_meta(root_id)" in helper_source
    assert "event_ingester.max_seq_by_sid(root_id)" not in helper_source
    assert "event_ingester.cursor(root_id)" not in helper_source
    assert "event_ingester.render_seq_by_sid(root_id)" not in helper_source

    ingester_source = (ROOT / "event_ingester.py").read_text(encoding="utf-8")
    assert "def session_event_meta(self, root_id: str)" in ingester_source
    scan_start = ingester_source.index("def _scan_max_seq(")
    scan_end = ingester_source.index("def close(", scan_start)
    scan_source = ingester_source[scan_start:scan_end]
    assert "summaries: dict[str, dict] = {}" in scan_source
    assert "self._update_summary_line(" in scan_source
    assert "self._summaries_cache[root_id] = (cur_offset, summaries, resolutions)" in scan_source


def test_event_summary_scan_reuses_full_scan_cache() -> None:
    source = (ROOT / "event_ingester.py").read_text(encoding="utf-8")
    max_start = source.index("def _scan_max_seq(")
    max_end = source.index("    @staticmethod\n    def _affects_render_projection", max_start)
    max_source = source[max_start:max_end]
    assert "all_entries: list[dict] = []" in max_source
    assert "self._full_scan_cache[root_id] = (cur_offset, all_entries)" in max_source
    assert "self._seq_offsets[root_id] = seq_offsets" in max_source

    summary_start = source.index("def _scan_summaries(")
    summary_end = source.index("    def close(", summary_start)
    summary_source = source[summary_start:summary_end]
    assert "cached = self._full_scan_cache.get(root_id)" in summary_source
    assert "entries = cached[1]" in summary_source
    assert "self._update_summary_line(" in summary_source
    assert "for index, entry in enumerate(entries):" in summary_source


def test_message_hydration_reuses_full_scan_cache() -> None:
    ingester_source = (ROOT / "event_ingester.py").read_text(encoding="utf-8")
    assert "def cached_rows_for_byte_range(" in ingester_source
    cache_start = ingester_source.index("def cached_rows_for_byte_range(")
    cache_end = ingester_source.index("def root_events_by_sid(", cache_start)
    cache_source = ingester_source[cache_start:cache_end]
    assert "cached = self._full_scan_cache.get(root_id)" in cache_source
    assert "line_start >= byte_end" in cache_source
    assert "rows.append(entry)" in cache_source

    journal_source = (ROOT / "event_journal.py").read_text(encoding="utf-8")
    owned_start = journal_source.index("def _read_owned_range(")
    owned_end = journal_source.index("def _read_raw_range(", owned_start)
    owned_source = journal_source[owned_start:owned_end]
    assert "event_ingester.cached_rows_for_byte_range(" in owned_source
    assert "if raw is None:" in owned_source
    assert "self._read_raw_range(" in owned_source


def test_metadata_session_search_uses_metadata_version_cache() -> None:
    source = (ROOT / "session_store.py").read_text(encoding="utf-8")
    assert "_metadata_search_cache" in source
    assert "_metadata_text_cache" in source
    assert "_metadata_text_cache: tuple[tuple[str, str, str], ...] = ()" in source
    assert "_summary_metadata_version" in source
    rows_start = source.index("def _metadata_search_rows(")
    rows_end = source.index("def _metadata_search_scores(", rows_start)
    rows_source = source[rows_start:rows_end]
    assert "str(summary.get(\"name\") or \"\").lower()" in rows_source
    assert "str(summary.get(\"first_prompt\") or \"\").lower()" in rows_source
    assert "_metadata_text_cache_version == _summary_metadata_version" in rows_source
    assert "return _metadata_text_cache" in rows_source
    assert "rows = tuple(" in rows_source
    assert "return list(_metadata_text_cache)" not in rows_source
    start = source.index("def _metadata_search_scores(")
    end = source.index("def grep_session_scores(", start)
    search_source = source[start:end]
    assert "cache_key = (query_lower, metadata_fields, _summary_metadata_version)" in search_source
    assert "cached = _metadata_search_cache.get(cache_key)" in search_source
    assert "return dict(cached)" in search_source
    assert "rows = _metadata_search_rows()" in search_source
    assert "title.count(query_lower)" in search_source
    assert "first_prompt.count(query_lower)" in search_source
    assert "_metadata_search_cache[cache_key] = dict(scores)" in search_source


def test_search_summary_lookup_uses_maintained_projection() -> None:
    source = (ROOT / "session_store.py").read_text(encoding="utf-8")
    start = source.index("def get_session_summaries_by_ids(")
    end = source.index("def iter_all_sessions()", start)
    lookup_source = source[start:end]
    assert "_requirement_tags_for_sessions" not in source
    assert "_markers_for_sessions" not in source
    assert "return [\n            _summary_index[sid]" in lookup_source


def test_sessions_response_cache_stores_serialized_bytes() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    cache_start = source.index("def _sessions_list_cache_get(")
    cache_end = source.index("_GIT_STATUS_TTL_SECONDS", cache_start)
    cache_source = source[cache_start:cache_end]
    assert "tuple[float, bytes, tuple[int, int, int]]" in source
    assert "return _sessions_list_response(cached[1])" in cache_source
    assert "json.dumps(" in cache_source
    assert "copy.deepcopy" not in cache_source
    assert "_SESSIONS_LIST_RESPONSE_TTL_SECONDS = 15.0" in source
    assert "def _sessions_list_transient_fingerprint(" not in source
    assert "def _sessions_list_transient_state_version()" in source
    assert "coordinator.turn_manager.cached_state_version()" in source
    assert "session_manager.unread_counts_version()" in source
    assert "user_input_store.pending_counts_version_loaded()" in source
    assert "cached[2] != _sessions_list_transient_state_version()" in cache_source


def test_sidebar_payload_reuses_summary_projection_cache() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "_sidebar_payload_cache" in source
    assert "_SIDEBAR_PAYLOAD_CACHE_MAX" in source
    assert "_sidebar_decorated_cache" in source
    assert "_SIDEBAR_DECORATED_CACHE_MAX" in source
    start = source.index("def _sidebar_session_payload(")
    end = source.index("def _sidebar_state_snapshot(", start)
    helper_source = source[start:end]
    assert "cache_key = id(session)" in helper_source
    assert "_sidebar_payload_cache.get(cache_key)" in helper_source
    assert "return cached[1]" in helper_source
    assert "_sidebar_payload_cache[cache_key] = (sid, payload)" in helper_source
    decorate_start = source.index("def _decorate_local_sidebar_sessions(")
    decorate_end = source.index("def _local_sessions_for_sidebar(", decorate_start)
    decorate_source = source[decorate_start:decorate_end]
    assert "decorated_cache_key = (" in decorate_source
    assert "id(s)," in decorate_source
    assert "pending_user_input_count," in decorate_source
    assert "_sidebar_decorated_cache.get(decorated_cache_key)" in decorate_source
    assert "_sidebar_decorated_cache[decorated_cache_key] = decorated" in decorate_source


def test_search_sessions_response_cache_uses_metadata_version() -> None:
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    helper_start = main_source.index("def _sessions_list_cache_version(")
    helper_end = main_source.index("_GIT_STATUS_TTL_SECONDS", helper_start)
    helper_source = main_source[helper_start:helper_end]
    assert "session_store.search_metadata_version()" in helper_source
    assert "session_search_index.generation()" in helper_source
    assert "session_store.SEARCH_FIELD_CONTENT in search_fields" in helper_source
    assert "session_store.summary_version()" in helper_source
    assert "virtual_session_store.version_token()" in helper_source
    route_start = main_source.index("async def get_sessions(")
    route_end = main_source.index("@app.post(\"/api/sessions/search-content\")", route_start)
    route_source = main_source[route_start:route_end]
    assert "_sessions_list_cache_version(search_query, effective_search_fields)" in route_source
    assert "_sessions_list_transient_state_version()" not in route_source
    cache_start = route_source.index("cache_key = (")
    cache_end = route_source.index(")", cache_start)
    cache_source = route_source[cache_start:cache_end]
    assert "cached_response = _sessions_list_cache_get(cache_key)" in route_source
    assert "search_query" in cache_source
    assert "\n        search,\n" not in cache_source
    assert "effective_search_fields = _split_session_search_fields(search_fields)" in route_source
    assert "tuple(sorted(effective_search_fields))" in route_source

    store_source = (ROOT / "session_store.py").read_text(encoding="utf-8")
    assert "def search_metadata_version()" in store_source
    assert "return _summary_metadata_version" in store_source


def test_session_list_waits_briefly_for_partial_summary_warm() -> None:
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "_SESSION_LIST_SUMMARY_WARM_WAIT_SECONDS = 0.08" in main_source
    assert "_SESSION_LIST_SUMMARY_WARM_MIN_PUBLISHED = 50" in main_source
    local_start = main_source.index("def _local_session_summaries_for_sidebar()")
    local_end = main_source.index(
        "def _local_session_summaries_by_ids_for_sidebar(",
        local_start,
    )
    local_source = main_source[local_start:local_end]
    assert "sessions.list.local.summary_warm_wait" in local_source
    assert (
        "min_published=_SESSION_LIST_SUMMARY_WARM_MIN_PUBLISHED"
        in local_source
    )

    store_source = (ROOT / "session_store.py").read_text(encoding="utf-8")
    wait_start = store_source.index("def wait_for_summary_index(")
    wait_end = store_source.index("def _replace_summary_projection_field(", wait_start)
    wait_source = store_source[wait_start:wait_end]
    assert "_ensure_summary_index(blocking=False)" in wait_source
    assert "min_published: int | None = None" in wait_source
    assert "len(_summary_index) >= target" in wait_source
    assert "_summary_build_lock.acquire(timeout=max(0.0, timeout_seconds))" in wait_source
    assert "_do_build_summary_index_unsafe()" not in wait_source


def test_sidebar_session_search_bounds_content_scoring() -> None:
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "_SESSION_LIST_CONTENT_SEARCH_MAX_WAIT_SECONDS" in main_source
    helper_start = main_source.index("async def _sidebar_search_scores(")
    helper_end = main_source.index("@app.get(\"/api/sessions\")", helper_start)
    helper_source = main_source[helper_start:helper_end]
    assert "if session_store.SEARCH_FIELD_CONTENT in selected_search_fields" in helper_source
    assert "metadata_max_wait_seconds" not in helper_source
    route_start = main_source.index("def _build_local_sessions_page_for_list(")
    route_end = main_source.index("@app.post(\"/api/sessions/search-content\")", route_start)
    route_source = main_source[route_start:route_end]
    assert route_source.count("content_max_wait_seconds = (") == 2
    assert "metadata_max_wait_seconds" not in route_source

    search_route_start = main_source.index("@app.post(\"/api/sessions/search-content\")")
    search_route_end = main_source.index("@app.post(\"/api/session-organization/query\")", search_route_start)
    search_route_source = main_source[search_route_start:search_route_end]
    assert "content_max_wait_seconds" not in search_route_source
    assert "metadata_max_wait_seconds" not in search_route_source


def test_pending_node_polling_uses_public_projection_cache() -> None:
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    route_start = main_source.index("async def internal_list_pending_nodes(")
    route_end = main_source.index("@app.post(\"/api/internal/machine-nodes/approve\")", route_start)
    route_source = main_source[route_start:route_end]
    assert "node_link.public_pending_nodes_cached()" in route_source
    assert "await asyncio.to_thread(node_link.public_pending_nodes)" in route_source
    assert "pending_node_registrations.list_pending()" not in route_source

    extension_source = (ROOT / "extension_api.py").read_text(encoding="utf-8")
    dispatch_start = extension_source.index("async def _dispatch_machine_nodes_core_backend(")
    dispatch_end = extension_source.index("async def _dispatch_project_structure_core_backend(", dispatch_start)
    dispatch_source = extension_source[dispatch_start:dispatch_end]
    assert "node_link.public_pending_nodes_cached()" in dispatch_source
    assert "await asyncio.to_thread(node_link.public_pending_nodes)" in dispatch_source


def test_machine_node_snapshot_reads_are_off_loop() -> None:
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    list_start = main_source.index("async def internal_get_nodes(")
    list_end = main_source.index("@app.get(\"/api/providers\")", list_start)
    list_source = main_source[list_start:list_end]
    pending_start = main_source.index("async def internal_list_pending_nodes(")
    pending_end = main_source.index("@app.post(\"/api/internal/machine-nodes/approve\")", pending_start)
    pending_source = main_source[pending_start:pending_end]
    assert "await asyncio.to_thread(node_store.snapshot)" in list_source
    assert "node_store.snapshot()" not in list_source
    assert "node_link.public_pending_nodes_cached()" in pending_source
    assert "await asyncio.to_thread(node_link.public_pending_nodes)" in pending_source

    extension_source = (ROOT / "extension_api.py").read_text(encoding="utf-8")
    dispatch_start = extension_source.index("async def _dispatch_machine_nodes_core_backend(")
    dispatch_end = extension_source.index("async def _dispatch_project_structure_core_backend(", dispatch_start)
    dispatch_source = extension_source[dispatch_start:dispatch_end]
    assert "await asyncio.to_thread(node_store.snapshot)" in dispatch_source
    assert "node_store.snapshot()" not in dispatch_source
    assert "node_link.public_pending_nodes_cached()" in dispatch_source
    assert "await asyncio.to_thread(node_link.public_pending_nodes)" in dispatch_source
    assert "_local_node_id_or_primary_cached()" in dispatch_source
    assert "await asyncio.to_thread(_local_node_id_or_primary" not in dispatch_source


def test_node_snapshot_caches_static_specs() -> None:
    source = (ROOT / "node_store.py").read_text(encoding="utf-8")
    assert "_snapshot_static_cache_key" in source
    assert "_snapshot_static_cache" in source
    assert "def _node_registry_fingerprint()" in source
    registry_source = (ROOT / "node_registry_store.py").read_text(encoding="utf-8")
    assert "def version_token()" in registry_source
    assert "node_registry_store.version_token()" in source
    assert "def _snapshot_static_specs()" in source
    snapshot_start = source.index("def snapshot()")
    snapshot_end = source.index("def connected_worker_node_ids_snapshot()", snapshot_start)
    snapshot_source = source[snapshot_start:snapshot_end]
    assert "specs = _snapshot_static_specs()" in snapshot_source
    assert "node_registry_store.list_all()" not in snapshot_source
    assert "load_topology().all_nodes()" not in snapshot_source


def test_pending_approval_listing_uses_cached_projection_off_loop() -> None:
    source = (ROOT / "stores" / "pending_approvals.py").read_text(encoding="utf-8")
    assert "_pending_cache_lock = threading.Lock()" in source
    assert "_pending_cache:" in source
    assert "def _invalidate_pending_cache()" in source
    assert "def _pending_snapshot()" in source
    list_start = source.index("def list_pending(")
    list_end = source.index("@perf.timed_fn(\"store.approval.transition\")", list_start)
    list_source = source[list_start:list_end]
    assert "records = _pending_snapshot()" in list_source
    assert "_dir().glob(\"*.json\")" not in list_source
    assert "path.read_text" not in list_source

    create_start = source.index("def create(")
    create_end = source.index("def get(", create_start)
    create_source = source[create_start:create_end]
    transition_start = source.index("def _transition_locked(")
    transition_end = source.index("def approve(", transition_start)
    transition_source = source[transition_start:transition_end]
    assert "_invalidate_pending_cache()" in create_source
    assert "_invalidate_pending_cache()" in transition_source

    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    route_start = main_source.index("async def internal_list_pending_approvals(")
    route_end = main_source.index("@app.post(\"/api/internal/tool-approvals/request\")", route_start)
    route_source = main_source[route_start:route_end]
    assert "await asyncio.to_thread(pending_approvals.list_pending, cwd=cwd)" in route_source
    assert "pending_approvals.list_pending(cwd=cwd)" not in route_source


def test_project_update_counts_batch_uses_single_store_call() -> None:
    store_source = (ROOT / "project_update_store.py").read_text(encoding="utf-8")
    assert "def unseen_counts(project_ids: list[str])" in store_source
    assert "def peek_unseen_counts(project_ids: list[str])" in store_source

    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    route_start = main_source.index("async def internal_project_update_counts_batch(")
    route_end = main_source.index("@app.post(\"/api/internal/project-updates/unseen\")", route_start)
    route_source = main_source[route_start:route_end]
    assert "counts = project_update_store.peek_unseen_counts(project_ids)" in route_source
    assert "if counts is None:" in route_source
    assert "await asyncio.to_thread(project_update_store.unseen_counts, project_ids)" in route_source
    assert "project_update_store.unseen_count(project_id)" not in route_source


def test_session_list_does_not_prewarm_snapshots() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "_schedule_session_snapshot_prewarm" not in source
    assert "sessions.snapshot_prewarm" not in source
    route_start = source.index("async def get_sessions(")
    route_end = source.index("@app.post(\"/api/sessions/search-content\")", route_start)
    route_source = source[route_start:route_end]
    assert "get_root_tree_stubbed" not in route_source
    assert "get_root_tree_paginated" not in route_source


def test_session_list_warms_event_meta_off_path() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "def _schedule_session_event_meta_warm(" in source
    assert "def _session_detail_projection_roots_for_page(" in source
    assert "def _warm_session_detail_projection_roots(" in source
    assert "await asyncio.to_thread(_warm_session_event_meta_roots_sync, pending)" in source
    assert "_SESSION_DETAIL_PAGE_WARM_DELAY_SECONDS" in source
    assert "_SESSION_DETAIL_PAGE_WARM_DELAY_SECONDS = 2.0" in source
    assert "_SESSION_DETAIL_PAGE_WARM_BATCH" in source
    assert "_SESSION_DETAIL_PAGE_WARM_BATCH = 1" in source
    assert "_SESSION_DETAIL_PAGE_WARM_BATCH_PAUSE_SECONDS" in source
    assert "_SESSION_DETAIL_PAGE_WARM_BATCH_PAUSE_SECONDS = 0.35" in source
    detail_warm_start = source.index("async def _warm_session_detail_projection_roots(")
    detail_warm_end = source.index("def _session_event_projection_warm_roots(", detail_warm_start)
    detail_warm_source = source[detail_warm_start:detail_warm_end]
    assert "def _session_detail_warm_cache_present(" in detail_warm_source
    assert "if _session_detail_warm_cache_present(root_id):" in detail_warm_source
    warm_present_start = detail_warm_source.index("def _session_detail_warm_cache_present(")
    warm_present_source = detail_warm_source[warm_present_start:]
    assert "_session_detail_response_cache_latest.get(simple_key)" in warm_present_source
    assert "_session_detail_response_cache_key_sync(" not in warm_present_source
    assert "await asyncio.sleep(_SESSION_DETAIL_PAGE_WARM_DELAY_SECONDS)" in detail_warm_source
    assert "await asyncio.to_thread(_warm_session_detail_projection_roots_sync, batch)" in detail_warm_source
    assert "await asyncio.sleep(_SESSION_DETAIL_PAGE_WARM_BATCH_PAUSE_SECONDS)" in detail_warm_source
    warm_start = source.index("def _schedule_session_event_meta_warm(")
    warm_end = source.index("def _machine_nodes_enabled_cached(", warm_start)
    warm_source = source[warm_start:warm_end]
    assert "_session_detail_projection_roots_for_page(page)" in warm_source
    assert "_warm_session_detail_projection_roots(projection_root_ids)" in warm_source
    assert "_session_event_file_fingerprint(" not in warm_source
    assert "_session_event_meta_cache_fresh(" not in warm_source
    roots_start = source.index("def _session_event_meta_roots_for_page(")
    roots_end = source.index("def _session_detail_projection_roots_for_page(", roots_start)
    roots_source = source[roots_start:roots_end]
    assert "_session_event_file_fingerprint(" not in roots_source
    detail_roots_start = roots_end
    detail_roots_end = source.index("async def _warm_session_event_meta_roots(", detail_roots_start)
    detail_roots_source = source[detail_roots_start:detail_roots_end]
    assert "_session_event_file_fingerprint(" not in detail_roots_source
    route_start = source.index("async def get_sessions(")
    route_end = source.index("@app.post(\"/api/sessions/search-content\")", route_start)
    route_source = source[route_start:route_end]
    assert "_schedule_session_event_meta_warm(page)" in route_source
    assert "_session_event_meta(" not in route_source


def test_session_list_reads_user_prefs_once() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "def _session_list_user_prefs(" in source
    assert "_session_list_user_prefs_cache" in source
    assert "_SESSION_LIST_USER_PREFS_TTL_SECONDS" in source
    prefs_start = source.index("def _session_list_user_prefs(")
    prefs_end = source.index("_GIT_STATUS_TTL_SECONDS", prefs_start)
    prefs_source = source[prefs_start:prefs_end]
    assert "time.monotonic()" in prefs_source
    assert "user_prefs.get_all()" in prefs_source
    route_start = source.index("async def get_sessions(")
    route_end = source.index("@app.post(\"/api/sessions/search-content\")", route_start)
    route_source = source[route_start:route_end]
    assert "_session_list_user_prefs()" in route_source
    assert "await asyncio.to_thread(_session_list_user_prefs)" not in route_source
    assert "user_prefs.get_folder_view_enabled()" not in route_source
    assert "user_prefs.get_session_sort()" not in route_source
    assert "user_prefs.get_session_status_sort()" not in route_source


def test_session_detail_has_split_perf_timers() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    route_start = source.index("async def get_session(")
    route_end = source.index("@app.get(\"/api/sessions/{session_id}/messages\")", route_start)
    route_source = source[route_start:route_end]
    helper_start = source.index("def _session_detail_snapshot_sync(")
    helper_end = source.index("def _floor_events_from_seq(", helper_start)
    helper_source = source[helper_start:helper_end]
    assert "await _run_session_detail_hot_path(\n        \"sessions.detail.worker\"" in route_source
    assert "await _run_hot_path(\n        \"sessions.detail.worker\"" not in route_source
    assert "session_manager.get_root_tree_stubbed" not in route_source
    assert 'perf.record("sessions.detail.worker"' not in route_source
    assert "return _json_bytes_response(tree)" in route_source
    json_response_start = source.index("def _json_bytes_response(")
    json_response_end = source.index("def _sessions_list_cache_get(", json_response_start)
    json_response_source = source[json_response_start:json_response_end]
    assert "separators=(\",\", \":\")" in json_response_source
    assert "Response(content=content, media_type=\"application/json\")" in json_response_source
    for timer in (
        "sessions.detail.event_meta",
        "sessions.detail.tree",
        "sessions.detail.strip_synthetic",
        "sessions.detail.reconcile_snapshot",
        "sessions.detail.max_context_copy",
        "sessions.detail.total",
    ):
        assert f'perf.record("{timer}"' in helper_source


def test_session_hot_paths_use_dedicated_executor_with_queue_wait_metrics() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    helper_start = source.index("async def _run_hot_path(")
    helper_end = source.index("def _latest_assistant_message_id(", helper_start)
    helper_source = source[helper_start:helper_end]
    assert "_HOT_PATH_EXECUTOR = ThreadPoolExecutor(" in source
    assert "max_workers=8" in source
    assert "thread_name_prefix=\"hot-path\"" in source
    assert "run_in_executor(\n            _HOT_PATH_EXECUTOR" in helper_source
    assert "_SESSION_DETAIL_EXECUTOR = ThreadPoolExecutor(" in source
    assert "thread_name_prefix=\"session-detail\"" in source
    assert "async def _run_session_detail_hot_path(" in helper_source
    assert "run_in_executor(\n            _SESSION_DETAIL_EXECUTOR" in helper_source
    assert 'perf.record(f"{name}.queue_wait"' in helper_source
    assert "perf.record(name," in helper_source

    route_start = source.index("async def get_sessions(")
    route_end = source.index("@app.post(\"/api/sessions/search-content\")", route_start)
    route_source = source[route_start:route_end]
    assert "await _run_hot_path(\n            \"sessions.list.local_page_thread\"" in route_source
    assert "await asyncio.to_thread(_build_local_sessions_page_for_list" not in route_source
    assert "await _run_hot_path(\n                    \"sessions.list.remote.local_order_candidates.worker\"" in route_source
    assert "\"sessions.list.page_decorate.worker\"" in route_source
    assert "await asyncio.to_thread(\n                _decorate_local_sidebar_sessions" not in route_source
    assert "await asyncio.to_thread(\n            _decorate_local_sidebar_sessions" not in route_source


def test_sidebar_summary_omits_worker_refs() -> None:
    source = (ROOT / "session_store.py").read_text(encoding="utf-8")
    start = source.index("def _build_summary_for_root(")
    end = source.index("def set_requirement_tags_projection(", start)
    build_source = source[start:end]
    assert "\"worker_count\"" in build_source
    assert "\"workers\"" not in build_source
    assert "def _sanitize_summary(" in source
    assert "summary, cleaned = _sanitize_summary(summary)" in source


def test_summary_worker_count_uses_count_projection() -> None:
    source = (ROOT / "session_store.py").read_text(encoding="utf-8")
    start = source.index("def _worker_summary_count()")
    end = source.index("def _refresh_summaries_for_cwd_from(", start)
    count_source = source[start:end]
    assert "worker_store.worker_count(\"\")" in count_source
    assert "worker_store.list_workers(\"\")" not in count_source

    worker_source = (ROOT / "stores" / "worker_store.py").read_text(encoding="utf-8")
    assert "_worker_count_cache" in worker_source
    assert "_WORKER_COUNT_HOT_TTL_SECONDS" in worker_source
    assert "now < _worker_count_cache_until" in worker_source
    assert "def worker_count(" in worker_source
    assert "_worker_count_cache.clear()" in worker_source


def test_summary_sidecar_stat_only_for_unchanged_summary() -> None:
    source = (ROOT / "session_store.py").read_text(encoding="utf-8")
    start = source.index("def _upsert_summary(")
    end = source.index("def _drafts_path(", start)
    upsert_source = source[start:end]
    assert "sidecar_current = True" in upsert_source
    assert "if not summary_changed:" in upsert_source
    assert "sidecar_current = _touch_summary_file_current(root[\"id\"])" in upsert_source
    assert "if summary_changed or not sidecar_current:" in upsert_source


def test_summary_index_skips_empty_projection_scan() -> None:
    source = (ROOT / "session_store.py").read_text(encoding="utf-8")
    assert "def _projection_snapshot()" in source
    assert "def _has_projection_snapshot()" not in source
    assert "def _start_summary_projection_repair(" in source
    assert "_summary_projection_repair_lock = threading.Lock()" in source
    assert "_summary_projection_repair_running = False" in source
    repair_start = source.index("def _start_summary_projection_repair()")
    repair_end = source.index("def summary_version()", repair_start)
    repair_source = source[repair_start:repair_end]
    assert "if _summary_projection_repair_running:" in repair_source
    assert "_summary_projection_repair_running = True" in repair_source
    assert "_summary_projection_repair_running = False" in repair_source
    assert "finally:" in repair_source
    assert "updates: dict[str, dict] = {}" in repair_source
    assert "projection_snapshot = _projection_snapshot()" in repair_source
    assert "_requirement_tags_for_session(sid)" not in repair_source
    assert "_markers_for_session(sid)" not in repair_source
    assert repair_source.count("_summary_index_version += 1") == 1
    build_start = source.index("def _do_build_summary_index_unsafe()")
    build_end = source.index("def _refresh_summaries_for_cwd(", build_start)
    build_source = source[build_start:build_end]
    cache_start = build_source.index("cached_summaries = _load_summary_index_cache(")
    pass_start = build_source.index("# Trees migrated in Pass 2")
    cache_source = build_source[cache_start:pass_start]
    assert "if _has_projection_snapshot()" not in cache_source
    assert "_start_summary_projection_repair()" in cache_source
    assert "return" in cache_source
    assert "projection_snapshot = _projection_snapshot()" in build_source
    assert "organization_projection = session_organization_store.enrichment_projection()" in build_source
    assert "_build_summary_for_root(" in build_source
    assert "organization_projection," in build_source
    assert "_start_summary_projection_repair()" in build_source
    assert "_summary_has_projection(" not in build_source
    assert "summary_projection_present" not in build_source
    assert "if _has_projection_snapshot()" not in build_source
    assert "summary_items = list(_summary_index.items())" not in build_source


def test_summary_index_validates_missing_summary_before_provider_context() -> None:
    source = (ROOT / "session_store.py").read_text(encoding="utf-8")
    build_start = source.index("def _do_build_summary_index_unsafe()")
    build_end = source.index("def _refresh_summaries_for_cwd(", build_start)
    build_source = source[build_start:build_end]
    assert "provider_ctx: Optional[dict] = None" in build_source
    parse_idx = build_source.index("raw = json.loads(fpath.read_text")
    validate_idx = build_source.index("if not isinstance(raw, dict) or \"id\" not in raw:")
    provider_idx = build_source.index("provider_ctx = _provider_backfill_context()")
    migrate_idx = build_source.index("data = _migrate_session(raw, provider_ctx)")
    assert parse_idx < validate_idx < provider_idx < migrate_idx


def test_summary_index_indexes_seen_sidecars_once() -> None:
    source = (ROOT / "session_store.py").read_text(encoding="utf-8")
    build_start = source.index("def _do_build_summary_index_unsafe()")
    build_end = source.index("def _refresh_summaries_for_cwd(", build_start)
    build_source = source[build_start:build_end]
    assert "seen_cursor_ids: set[str] = set()" in build_source
    assert "for p in _sessions_dir().iterdir():" in build_source
    assert ".glob(\"*.summary.json\")" not in build_source
    assert ".glob(\"*.seen.json\")" not in build_source
    assert "read_seen_cursors(sid) if sid in seen_cursor_ids else {}" in build_source
    assert "_summary_index_cache_fingerprint(" in build_source
    assert "_load_summary_index_cache(summary_cache_fingerprint)" in build_source
    assert "_write_summary_index_cache(summary_cache_fingerprint, summaries)" in build_source
    assert "\"skipped_root_ids\"" in source


def test_summary_index_cache_is_sidecar() -> None:
    source = (ROOT / "session_store.py").read_text(encoding="utf-8")
    assert "def _summary_index_cache_path()" in source
    assert "\".summary-index.json\"" in source
    sidecar_start = source.index("_SIDECAR_JSON_SUFFIXES = (")
    sidecar_end = source.index("def _is_sidecar_json", sidecar_start)
    sidecar_source = source[sidecar_start:sidecar_end]
    assert "\".summary-index.json\"" in sidecar_source


def test_session_store_sessions_dir_is_cached() -> None:
    source = (ROOT / "session_store.py").read_text(encoding="utf-8")
    assert "_SESSIONS_DIR = ba_home() / \"sessions\"" in source
    sessions_dir_start = source.index("def _sessions_dir()")
    sessions_dir_end = source.index("def _ensure_dir()", sessions_dir_start)
    sessions_dir_source = source[sessions_dir_start:sessions_dir_end]
    assert "return _SESSIONS_DIR" in sessions_dir_source
    assert "ba_home()" not in sessions_dir_source


def test_event_journal_watch_path_uses_cached_sessions_dir() -> None:
    source = (ROOT / "event_journal.py").read_text(encoding="utf-8")
    assert "def _sessions_dir()" in source
    assert "_SESSIONS_DIR_CACHE" in source
    read_start = source.index("def _read_appended_entries(")
    read_end = source.index("def read_events(", read_start)
    read_source = source[read_start:read_end]
    assert "_sessions_dir() / session_id / \"events.jsonl\"" in read_source
    assert "ba_home()" not in read_source


def test_run_state_emit_debug_logging_is_gated() -> None:
    source = (ROOT / "turn_manager.py").read_text(encoding="utf-8")
    start = source.index("def _dbg_runstate(")
    end = source.index("# ======================================================================", start)
    run_state_source = source[start:end]
    assert "logger.isEnabledFor(logging.DEBUG)" in run_state_source
    assert "logger.debug(" in run_state_source
    assert "logger.info(" not in run_state_source
    assert "await self._c.broadcast_session" in run_state_source


def test_startup_session_search_rebuild_skips_persisted_index() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    startup_start = source.index("async def on_startup()")
    startup_end = source.index("async def on_shutdown()", startup_start)
    startup_source = source[startup_start:startup_end]
    assert "session_search_index.needs_rebuild()" in startup_source


def test_event_projections_warm_in_background() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "def _session_event_projection_warm_roots(" in source
    assert "def _warm_session_detail_projection_roots_sync(" in source
    assert "async def _warm_session_event_projections()" in source
    assert "await asyncio.to_thread(\n        _session_event_projection_warm_roots" in source
    detail_warm_start = source.index("def _warm_session_detail_projection_roots_sync(")
    detail_warm_end = source.index("def _session_event_projection_warm_roots(", detail_warm_start)
    detail_warm_source = source[detail_warm_start:detail_warm_end]
    assert "_session_event_meta(root_id)" in detail_warm_source
    assert "session_store.get_session_summaries_by_ids([root_id])" in detail_warm_source
    assert "message_count" in detail_warm_source
    assert "if not summaries or int(summaries[0].get(\"message_count\") or 0) > 0:" in detail_warm_source
    assert "event_ingester.message_event_summaries(root_id)" in detail_warm_source
    assert "_session_detail_response_cache_key_sync(" in detail_warm_source
    assert "_session_detail_cache_has(cache_key)" in detail_warm_source
    assert "_session_detail_snapshot_sync(" in detail_warm_source
    assert detail_warm_source.index("_session_detail_cache_has(cache_key)") < detail_warm_source.index("event_ingester.message_event_summaries(root_id)")
    assert detail_warm_source.index("_session_detail_cache_has(cache_key)") < detail_warm_source.index("_session_detail_snapshot_sync(")
    assert "_session_detail_cache_put(cache_key, tree)" in detail_warm_source
    assert "_SESSION_DETAIL_WARM_EXCHANGE_COUNT" in detail_warm_source
    roots_start = source.index("def _session_event_projection_warm_roots(")
    roots_end = source.index("async def _warm_session_event_projections()", roots_start)
    roots_source = source[roots_start:roots_end]
    assert "events_path = child / \"events.jsonl\"" in roots_source


def test_render_hydrate_worker_fingerprint_is_batched() -> None:
    source = (ROOT / "render_tree_hydrate.py").read_text(encoding="utf-8")
    start = source.index("            pre_worker_fingerprint = (")
    end = source.index("            for raw in orphan_rows:", start)
    worker_source = source[start:end]
    assert "before_worker" not in worker_source
    assert worker_source.count("_message_timeline_fingerprint(m)") == 2
    assert "pre_worker_fingerprint is not None" in worker_source


def test_session_detail_cache_hit_validation_uses_cheap_fingerprint() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    helper_start = source.index("def _session_detail_cached_key_still_current(")
    helper_end = source.index("def _floor_events_from_seq(", helper_start)
    helper_source = source[helper_start:helper_end]
    assert "session_manager.root_tree_stub_cache_key(" in helper_source
    assert "_session_event_file_fingerprint(root_id)" in helper_source
    assert "_session_event_meta(" not in helper_source
    assert "_session_detail_response_cache_key_sync(" not in helper_source

    route_start = source.index("async def get_session(")
    route_end = source.index("@app.get(\"/api/sessions/{session_id}/messages\")", route_start)
    route_source = source[route_start:route_end]
    assert "_session_detail_cached_key_still_current" in route_source
    assert "_session_detail_response_cache_key_sync" not in route_source[
        route_source.index("if cached_full_key is not None:"):
        route_source.index("perf.record(\"sessions.detail.response_cache.miss\"",)
    ]
    assert "meta_path" not in roots_source
    warm_start = source.index("async def _warm_session_event_projections()")
    warm_end = source.index("def _schedule_session_event_meta_warm(", warm_start)
    warm_source = source[warm_start:warm_end]
    assert "_SESSION_EVENT_META_GLOBAL_WARM_BATCH" in warm_source
    assert "await asyncio.to_thread(_warm_session_detail_projection_roots_sync, batch)" in warm_source
    assert "await asyncio.sleep(_SESSION_EVENT_META_GLOBAL_WARM_BATCH_PAUSE_SECONDS)" in warm_source
    startup_start = source.index("async def on_startup()")
    startup_end = source.index("async def on_shutdown()", startup_start)
    startup_source = source[startup_start:startup_end]
    assert "startup-session-event-meta-projection-warm" in startup_source
    assert "session_event_projection_warm" in startup_source
    assert "_SESSION_EVENT_META_GLOBAL_WARM_DELAY_SECONDS" in startup_source
    assert "_rebuild_session_search_index_if_empty" in startup_source


def test_project_match_rebuild_skips_unchanged_session_state() -> None:
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    warm_start = main_source.index("async def _project_match_warm_loop()")
    warm_end = main_source.index("def _ensure_project_match_warm_task()", warm_start)
    warm_source = main_source[warm_start:warm_end]
    assert "fingerprint = None" in warm_source
    assert "rebuild_index,\n                fingerprint," in warm_source
    assert 'result.get("fingerprint")' in warm_source
    assert 'result.get("rebuilt") is False' in warm_source

    worker_source = (ROOT / "project_match" / "worker.py").read_text(encoding="utf-8")
    assert "def sessions_fingerprint()" in worker_source
    assert "previous_fingerprint is not None and fingerprint == previous_fingerprint" in worker_source
    assert '{"rebuilt": False, "fingerprint": fingerprint}' in worker_source


def test_stubbed_tree_cache_key_does_not_scan_message_events() -> None:
    source = (ROOT / "session_manager.py").read_text(encoding="utf-8")
    key_start = source.index("def _tree_stub_cache_key(")
    key_end = source.index("def _build_stubbed_tree(", key_start)
    key_source = source[key_start:key_end]
    assert "render_seq_by_sid = event_ingester.render_seq_by_sid(rid)" in key_source
    assert 'msg.get("events")' not in key_source
    assert "event_shape" not in key_source
    assert "root_events_version" not in key_source


def test_stubbed_tree_cache_attaches_root_events_after_cache_copy() -> None:
    source = (ROOT / "session_manager.py").read_text(encoding="utf-8")
    build_start = source.index("def _build_stubbed_tree(")
    build_end = source.index("def _compute_messages_snapshot(", build_start)
    build_source = source[build_start:build_end]
    assert "self._attach_root_events_to_stubbed_tree(tree, rid)" in build_source
    assert build_source.index("tree = _copy_jsonish(cached)") < build_source.index(
        "self._attach_root_events_to_stubbed_tree(tree, rid)"
    )
    assert build_source.index("self._tree_stub_cache[cache_key] = _copy_jsonish(tree)") < build_source.rindex(
        "self._attach_root_events_to_stubbed_tree(tree, rid)"
    )


def test_startup_recovery_defers_cold_runs() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    recover_start = source.index("async def _recover_in_flight_task()")
    recover_end = source.index("async def _housekeeping_task()", recover_start)
    recover_source = source[recover_start:recover_end]
    assert "live = [r for r in recovered if bool(r.get(\"alive\"))]" in recover_source
    assert "cold = [r for r in recovered if not bool(r.get(\"alive\"))]" in recover_source
    assert "_delayed_recovered_run_integration(cold)" in recover_source


def test_startup_recovery_gate_opens_before_live_integration() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    recover_start = source.index("async def _recover_in_flight_task()")
    recover_end = source.index("async def _housekeeping_task()", recover_start)
    recover_source = source[recover_start:recover_end]
    assert recover_source.index("startup_recovery_gate.mark_recovery_done()") < recover_source.index(
        "await integrate_recovered_runs(coordinator, live)"
    )


def test_hydration_uses_local_projection_not_extension_backend() -> None:
    source = (ROOT / "session_manager.py").read_text(encoding="utf-8")
    hydrate_start = source.index("    def _derive_current_todos_from_events_jsonl(")
    hydrate_end = source.index("    def _cached(", hydrate_start)
    hydrate_source = source[hydrate_start:hydrate_end]
    assert "session_local_projection.project_event_fields(" in hydrate_source
    assert "session_event_extensions" not in hydrate_source
    assert "extension_backend_loader" not in hydrate_source


def test_session_event_extension_callbacks_are_worker_only() -> None:
    source = (ROOT / "session_event_extensions.py").read_text(encoding="utf-8")
    project_start = source.index("def project_event(")
    project_end = source.index("def _apply_builtin_event(", project_start)
    project_source = source[project_start:project_end]
    apply_start = source.index("def apply_event(")
    apply_end = source.index("def _apply_event_locked(", apply_start)
    apply_source = source[apply_start:apply_end]
    worker_start = source.index("def _run_extension_hook_job(")
    worker_end = source.index("def _run_builtin_todos_job(", worker_start)
    worker_source = source[worker_start:worker_end]
    assert "invoke_extension_backend_sync" not in project_source
    assert "invoke_extension_backend_sync" not in apply_source
    assert "invoke_extension_backend_sync" in worker_source


def test_session_event_apply_event_uses_cached_hook_snapshot() -> None:
    source = (ROOT / "session_event_extensions.py").read_text(encoding="utf-8")
    apply_start = source.index("def apply_event(")
    apply_end = source.index("def _apply_event_locked(", apply_start)
    apply_source = source[apply_start:apply_end]
    assert "hook_snapshot_nonblocking()" in apply_source
    assert "hook_snapshot()" not in apply_source
    assert "session_event_hook_specs()" not in apply_source
    assert "_builtin_todos_enabled()" not in apply_source


def test_requirement_tag_refresh_is_off_startup_loop() -> None:
    subscribers_source = (ROOT / "event_bus_subscribers.py").read_text(encoding="utf-8")
    refresh_start = subscribers_source.index("async def _refresh_requirement_tags(")
    refresh_end = subscribers_source.index("async def _apply_requirement_tags_projection(", refresh_start)
    refresh_source = subscribers_source[refresh_start:refresh_end]
    assert "await asyncio.to_thread(_refresh_requirement_tags_sync)" in refresh_source
    assert "ModuleNotFoundError" in refresh_source

    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    startup_start = main_source.index("async def on_startup()")
    startup_end = main_source.index("async def on_shutdown()", startup_start)
    startup_source = main_source[startup_start:startup_end]
    assert 'name="requirement-tags-startup-refresh"' not in startup_source
    assert 'type="requirement_tags.refresh_requested"' not in startup_source
    assert 'await event_bus.publish(BusEvent(\\n            type="requirement_tags.refresh_requested"' not in startup_source
    assert "ModuleNotFoundError" in startup_source


def test_machine_nodes_readiness_check_is_off_startup_loop() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    startup_start = source.index("async def on_startup()")
    startup_end = source.index("async def on_shutdown()", startup_start)
    startup_source = source[startup_start:startup_end]
    assert "async def _start_node_offset_loop_if_ready()" in startup_source
    assert "await asyncio.to_thread(\n                extension_store.is_extension_runtime_ready" in startup_source
    assert 'name="node-offset-flush-startup"' in startup_source


def test_sessions_route_does_not_runtime_check_machine_nodes() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    route_start = source.index('@app.get("/api/sessions")')
    route_end = source.index('@app.get("/api/sessions/{session_id}")', route_start)
    route_source = source[route_start:route_end]
    assert "connected_worker_node_ids_snapshot()" in route_source
    assert "_ns.snapshot()" not in route_source
    assert "sessions.list.node_snapshot" not in route_source
    assert "_builtin_extension_runtime_ready_fast" not in route_source
    assert "_builtin_extension_runtime_ready(" not in route_source


def test_sessions_route_uses_cached_remote_node_sessions() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    helper_start = source.index("async def _remote_sessions_for_sidebar(")
    helper_end = source.index("def _session_list_user_prefs(", helper_start)
    helper_source = source[helper_start:helper_end]
    route_start = source.index('@app.get("/api/sessions")')
    route_end = source.index('@app.get("/api/sessions/{session_id}")', route_start)
    route_source = source[route_start:route_end]
    assert "_REMOTE_SESSIONS_CACHE_TTL_SECONDS = 2.0" in source
    assert "def _remote_sessions_cache_get(\n    node_id: str," in source
    assert "limit: int | None = None" in source
    assert "def _schedule_remote_sessions_refresh(node_id: str)" in source
    assert "async def _fetch_remote_sessions_live(node_id: str)" in source
    assert "sessions.list.remote_cache.hit" in helper_source
    assert "sessions.list.remote_cache.stale" in helper_source
    assert "sessions.list.remote_cache.miss" in helper_source
    assert "_remote_sessions_cache_version_snapshot() if connected else 0" in route_source
    assert "with perf.timed(\"sessions.list.remote\")" in route_source
    assert "_remote_sessions_for_sidebar(nid)" in route_source
    assert "rs[\"node_id\"] = nid" in route_source


def test_connected_session_list_defers_cold_sidebar_projections() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    route_start = source.index('@app.get("/api/sessions")')
    route_end = source.index('@app.get("/api/sessions/{session_id}")', route_start)
    route_source = source[route_start:route_end]
    remote_helper_start = source.index("def _remote_sessions_for_sidebar_cached(")
    remote_helper_end = source.index("def _schedule_virtual_sessions_recent_refresh(", remote_helper_start)
    remote_helper_source = source[remote_helper_start:remote_helper_end]
    virtual_helper_start = source.index("def _schedule_virtual_sessions_recent_refresh(")
    virtual_helper_end = source.index("def _session_list_user_prefs(", virtual_helper_start)
    virtual_helper_source = source[virtual_helper_start:virtual_helper_end]
    virtual_store_source = (ROOT / "virtual_session_store.py").read_text(encoding="utf-8")
    assert "def list_recent_cached(" in virtual_store_source
    assert "sessions.list.remote_cache.deferred_miss" in remote_helper_source
    assert "_schedule_remote_sessions_refresh(node_id)" in remote_helper_source
    assert "asyncio.to_thread(\n            virtual_session_store.list_recent," in virtual_helper_source
    assert "sessions.list.virtual.cached_first_page" in route_source
    assert "_remote_sessions_for_sidebar_cached(\n                        nid," in route_source
    assert "limit=max(offset + limit, 1)" in route_source
    assert "deferred_sidebar_projection and not appended_virtual_sessions and not appended_remote_sessions" in route_source
    assert "projected_first_page_sessions" in route_source
    assert "sessions.list.projected_first_page_merge" in route_source
    assert route_source.index("sessions.list.projected_first_page_merge") < route_source.index(
        'with perf.timed("sessions.list.filter_sort")'
    )
    assert "_sessions_list_response(\n                    json.dumps(" in route_source


def test_default_session_page_uses_visible_order_cache() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    helper_start = source.index("def _local_visible_order_ids(")
    helper_end = source.index("def _local_session_page_for_sidebar_preserving_order(", helper_start)
    helper_source = source[helper_start:helper_end]
    assert "_local_visible_order_cache" in source
    assert "session_store.summary_version()" in helper_source
    assert "sessions.list.local.visible_order_cache.hit" in helper_source
    assert "sessions.list.local.visible_order_build" in helper_source

    page_start = source.index("def _local_session_page_for_sidebar_preserving_order(")
    page_end = source.index("def _root_session_file_path(", page_start)
    page_source = source[page_start:page_end]
    assert "_can_page_default_local_visible_order(" in page_source
    assert "sessions.list.local.visible_order_page" in page_source
    assert "visible_ids[offset:offset + limit]" in page_source
    assert page_source.index("sessions.list.local.visible_order_page") < page_source.index(
        "sessions.list.local.ordered_ids"
    )


def test_session_search_uses_bounded_candidate_window() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    helper_start = source.index("def _session_search_candidate_limit(")
    helper_end = source.index("@app.get(\"/api/sessions\")", helper_start)
    helper_source = source[helper_start:helper_end]
    assert "_SESSION_LIST_SEARCH_MIN_CANDIDATES = 200" in source
    assert "max(offset + limit, _SESSION_LIST_SEARCH_MIN_CANDIDATES)" in helper_source

    route_start = source.index("@app.get(\"/api/sessions\")")
    route_end = source.index("@app.post(\"/api/sessions/search-content\")", route_start)
    route_source = source[route_start:route_end]
    assert "content_limit=_session_search_candidate_limit(offset, limit)" in route_source
    assert "content_limit=max(offset + limit, 1)" not in route_source
    assert "cache_response = not search_query" in route_source
    assert "_sessions_list_cache_get(cache_key) if cache_response else None" in route_source


def test_session_organization_refresh_is_coalesced_background_work() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    helper_start = source.index("async def _broadcast_session_organization_changed(")
    helper_end = source.index("async def _apply_initial_session_folder(", helper_start)
    helper_source = source[helper_start:helper_end]
    assert "_session_organization_refresh_pending = True" in helper_source
    assert "asyncio.create_task(_refresh_loop())" in helper_source
    assert "await asyncio.to_thread(session_store.refresh_organization_projection, session_ids)" in helper_source
    assert "if _session_organization_refresh_task is not None and not _session_organization_refresh_task.done()" in helper_source


def test_get_session_strips_synthetic_events_off_loop() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "def _tree_has_loaded_events(" in source
    route_start = source.index("async def get_session(")
    route_end = source.index("@app.get(\"/api/sessions/{session_id}/messages\")", route_start)
    route_source = source[route_start:route_end]
    helper_start = source.index("def _session_detail_snapshot_sync(")
    helper_end = source.index("def _floor_events_from_seq(", helper_start)
    helper_source = source[helper_start:helper_end]
    assert "_strip_synthetic_events_from_tree(tree)" not in route_source
    assert "if _tree_has_loaded_events(tree):" in helper_source
    assert "_strip_synthetic_events_from_tree(tree)" in helper_source
    assert "strip_ms" in helper_source


def test_session_detail_response_bytes_are_cached() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "_session_detail_response_cache" in source
    assert "_SESSION_DETAIL_RESPONSE_CACHE_MAX = 64" in source
    assert "def _session_detail_cache_get(" in source
    assert "def _session_detail_cache_put(" in source
    assert "def _session_detail_response_cache_key_sync(" in source
    assert "_SESSION_DETAIL_RESPONSE_TTL_SECONDS" not in source
    cache_get_start = source.index("def _session_detail_cache_get(")
    cache_get_end = source.index("def _session_detail_cache_put(", cache_get_start)
    cache_get_source = source[cache_get_start:cache_get_end]
    assert "time.monotonic()" not in cache_get_source
    route_start = source.index("async def get_session(")
    route_end = source.index("@app.get(\"/api/sessions/{session_id}/messages\")", route_start)
    route_source = source[route_start:route_end]
    assert "_session_detail_cache_get(cache_key)" in route_source
    assert "_session_detail_response_cache_latest.get(simple_cache_key)" in route_source
    assert "if cached_full_key is not None:" in route_source
    assert "_session_reconcile_snapshot_and_schedule" in route_source
    assert "include_cache_key=True" in route_source
    assert "_session_detail_cache_put(cache_key, tree)" in route_source


def test_stubbed_tree_cache_covers_broad_session_loads() -> None:
    source = (ROOT / "session_manager.py").read_text(encoding="utf-8")
    assert "self._tree_stub_cache_max = 256" in source


def test_run_recovery_finalize_session_manager_calls_are_off_loop() -> None:
    source = (ROOT / "run_recovery.py").read_text(encoding="utf-8")
    finalize_start = source.index("async def _finalize_when_done(")
    finalize_end = source.index("# ============================================================================", finalize_start)
    finalize_source = source[finalize_start:finalize_end]
    assert "await asyncio.to_thread(\n            _recovery_target_snapshot" in finalize_source
    assert "await asyncio.to_thread(\n                    session_manager.set_msg_recovering" in finalize_source
    assert "session_manager.get(persist_sid)" not in finalize_source
    assert "session_manager.set_msg_recovering(persist_sid" not in finalize_source


def test_run_recovery_summarizes_repeated_skip_logs() -> None:
    source = (ROOT / "run_recovery.py").read_text(encoding="utf-8")
    assert "class _RecoveryLogSummary:" in source
    assert "summary.record_skip(\"missing target_message_id\", run_id)" in source
    assert "summary.record_not_marked(reason, run_id)" in source
    assert "summary.emit()" in source
    assert "integrate_recovered_runs: skip %s (missing target_message_id)" in source
    assert "integrate_recovered_runs: skipped %d run(s): %s%s" in source


def test_extension_backend_get_skips_body_stream() -> None:
    source = (ROOT / "extension_backend_loader.py").read_text(encoding="utf-8")
    assert '_METHODS_WITH_REQUEST_BODY = {"POST", "PUT", "PATCH", "DELETE"}' in source
    dispatch_start = source.index("async def dispatch_extension_backend_request(")
    dispatch_end = source.index("async def invoke_extension_backend(", dispatch_start)
    dispatch_source = source[dispatch_start:dispatch_end]
    assert 'method = str(getattr(request, "method", "POST") or "POST").upper()' in dispatch_source
    assert "if method in _METHODS_WITH_REQUEST_BODY" in dispatch_source
    assert "else b\"\"" in dispatch_source


def test_extension_backend_invoke_has_split_perf_timers() -> None:
    source = (ROOT / "extension_backend_loader.py").read_text(encoding="utf-8")
    assert "_EMPTY_B64 = \"\"" in source
    start = source.index("async def _invoke_backend(")
    end = source.index("async def dispatch_extension_backend_request(", start)
    invoke_source = source[start:end]
    for timer in (
        "extension.backend.invoke.payload",
        "extension.backend.invoke.handle",
        "extension.backend.invoke.timeout",
        "extension.backend.invoke.roundtrip",
        "extension.backend.invoke.decode",
        "extension.backend.invoke.response",
    ):
        assert timer in invoke_source
    assert "body_b64 = (" in invoke_source
    assert "if body_bytes" in invoke_source
    dispatch_start = source.index("async def dispatch_extension_backend_request(")
    dispatch_end = source.index("async def invoke_extension_backend(", dispatch_start)
    dispatch_source = source[dispatch_start:dispatch_end]
    assert "else _EMPTY_B64" in dispatch_source


def test_builtin_extension_core_dispatch_precedes_backend_spec_lookup() -> None:
    source = (ROOT / "extension_api.py").read_text(encoding="utf-8")
    dispatch_start = source.index("async def dispatch_backend_extension(")
    dispatch_end = source.index("async def _dispatch_core_builtin_backend(", dispatch_start)
    dispatch_source = source[dispatch_start:dispatch_end]
    assert dispatch_source.index("_dispatch_core_builtin_backend(") < dispatch_source.index(
        "backend_entrypoint_spec_cached("
    )
    core_start = source.index("async def _dispatch_core_builtin_backend(")
    core_end = source.index("async def _dispatch_machine_nodes_core_backend(", core_start)
    core_source = source[core_start:core_end]
    assert "extension_id != extension_store.BUILTIN_MACHINE_NODES_EXTENSION_ID" in core_source
    assert "extension_store.BUILTIN_TEAM_ORCHESTRATION_EXTENSION_ID" in core_source
    assert "extension_id != extension_store.BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID" in core_source
    assert "extension_store.is_extension_enabled_cached(extension_id)" in core_source
    team_start = source.index("async def _dispatch_team_orchestration_core_backend(")
    team_end = source.index("async def _dispatch_machine_nodes_core_backend(", team_start)
    team_source = source[team_start:team_end]
    assert 'request.method == "GET" and path == "workers"' in team_source
    assert 'request.method == "GET" and path == "pending_approvals"' in team_source
    assert "team_orchestration_read.list_workers_for_cwd" in team_source
    assert "pending_approvals.list_pending" in team_source
    project_start = source.index("async def _dispatch_project_structure_core_backend(")
    project_end = source.index("@router.post(\"/install\")", project_start)
    project_source = source[project_start:project_end]
    assert 'request.method == "GET" and path == "project-updates/total"' in project_source
    assert 'request.method != "POST" or path != "project-updates/counts-batch"' in project_source
    assert "project_update_store.peek_total_unseen()" in project_source
    assert "project_update_store.peek_unseen_counts(project_ids)" in project_source
    assert "await asyncio.to_thread(project_update_store.total_unseen)" in project_source
    assert "await asyncio.to_thread(project_update_store.unseen_counts, project_ids)" in project_source


def test_project_update_total_is_maintained_projection() -> None:
    source = (ROOT / "project_update_store.py").read_text(encoding="utf-8")
    assert "_total_unseen_count = 0" in source
    assert "_counts_version = 0" in source
    assert "def version_token(" in source
    assert "def warm_counts(" in source
    load_start = source.index("def _ensure_counts_locked(")
    load_end = source.index("def _set_count_locked(", load_start)
    load_source = source[load_start:load_end]
    set_start = source.index("def _set_count_locked(")
    set_end = source.index("def append(", set_start)
    set_source = source[set_start:set_end]
    total_start = source.index("def total_unseen(")
    total_end = source.index("def mark_seen(", total_start)
    total_source = source[total_start:total_end]
    append_start = source.index("def append(")
    append_end = source.index("def list_unseen(", append_start)
    append_source = source[append_start:append_end]
    mark_start = source.index("def mark_seen(")
    mark_end = source.index("def list_all(", mark_start)
    mark_source = source[mark_start:mark_end]
    assert "_total_unseen_count = total" in load_source
    assert "_read_entries_path_locked(path)" in load_source
    assert "_read_entries_locked(path.stem)" not in load_source
    assert "def _project_path(project_id: str, *, create_dir: bool = True)" in source
    assert "_project_path(project_id, create_dir=False)" in source
    assert "if count == previous:\n        return" in set_source
    assert "_total_unseen_count += count - previous" in set_source
    assert "_total_unseen_count -= previous" in set_source
    assert "_counts_version += 1" in set_source
    assert "_set_count_locked(project_id, _unseen_counts.get(project_id, 0) + 1)" in append_source
    assert "_set_count_locked(project_id, _unseen_counts.get(project_id, 0) - count)" in mark_source
    assert "return _total_unseen_count" in total_source
    assert "sum(_unseen_counts.values())" not in total_source

    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    startup_start = main_source.index("async def on_startup()")
    startup_end = main_source.index("async def on_shutdown()", startup_start)
    startup_source = main_source[startup_start:startup_end]
    assert '"project_update_counts_warm"' in startup_source
    assert "project_update_store.warm_counts" in startup_source
    assert 'name="startup-project-update-counts-warm"' in startup_source


def test_builtin_feature_enabled_has_cached_projection() -> None:
    source = (ROOT / "extension_store.py").read_text(encoding="utf-8")
    assert "_BUILTIN_FEATURE_CACHE" in source
    assert "def is_builtin_feature_enabled_cached(" in source
    store_path_start = source.index("def _store_path(")
    store_path_end = source.index("def store_fingerprint(", store_path_start)
    store_path_source = source[store_path_start:store_path_end]
    assert "_STORE_PATH" in source
    assert "if _STORE_PATH is None:" in store_path_source
    assert "ba_home()" in store_path_source
    start = source.index("def is_builtin_feature_enabled_cached(")
    end = source.index("def is_extension_runtime_ready(", start)
    helper_source = source[start:end]
    assert "fingerprint = store_fingerprint()" in helper_source
    assert "_BUILTIN_FEATURE_CACHE.get(extension_id)" in helper_source
    assert "is_builtin_feature_enabled(extension_id)" in helper_source


def test_extension_list_reconciliation_is_off_loop() -> None:
    source = (ROOT / "extension_api.py").read_text(encoding="utf-8")
    route_start = source.index("async def list_extensions(")
    route_end = source.index("@router.get(\"/builtin-ids\")", route_start)
    route_source = source[route_start:route_end]
    assert 'cache_key = (extension_store.store_fingerprint(), include_hidden)' in route_source
    assert '_projection_response_cache_get("list", cache_key)' in route_source
    assert "await asyncio.to_thread(\n        extension_store.list_extensions_with_reconciliation" in route_source
    assert "extensions, changed = extension_store.list_extensions_with_reconciliation" not in route_source
    assert '_projection_response_cache_put("list", cache_key, {"extensions": extensions})' in route_source


def test_internal_communication_worker_lookup_is_off_loop() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    resolver_start = source.index("async def _resolve_communication_target(")
    resolver_end = source.index("@app.post(\"/api/internal/ask\")", resolver_start)
    resolver_source = source[resolver_start:resolver_end]
    assert "await asyncio.to_thread(_find_worker_by_agent_session_id" in resolver_source
    assert "await asyncio.to_thread(\n        _pick_pool_worker_for_sender" in resolver_source

    async_start = source.index("async def _ask_continue_and_expect_mssg_back_async(")
    async_end = source.index("async def _ask_wait_and_grab_last_mssg_in_turn(", async_start)
    async_source = source[async_start:async_end]
    assert "await asyncio.to_thread(\n            _pick_pool_worker_for_sender" in async_source
    assert "await _resolve_communication_target(body)" in async_source
    assert "target = _pick_idle_pool_worker(target_worker_pool)" not in async_source


if __name__ == "__main__":
    test_hook_runner_loads_config_off_loop()
    test_ownership_projection_uses_dedicated_executor()
    test_wire_tailer_gap_fill_reads_journal_off_loop()
    test_jsonl_dispatch_reads_session_lite_off_loop()
    test_jsonl_fallback_followers_poll_files_off_loop()
    test_subagent_watcher_scans_files_off_loop()
    test_delegation_locked_reuses_worker_session_snapshot()
    test_jsonl_dispatch_ingests_orphans_off_loop()
    test_wire_tailer_subscribe_resolves_root_off_loop()
    test_native_demand_publish_does_not_leak_coroutine_without_loop()
    test_wire_tailer_unsubscribe_uses_cached_subscriber_root()
    test_root_session_write_does_not_resolve_root_id()
    test_session_first_prompt_search_uses_summary_index()
    test_session_content_search_aggregates_in_sqlite()
    test_session_search_delete_is_queued_projection_work()
    test_publish_event_default_path_skips_temp_ack_subscribers()
    test_connected_session_list_skips_full_sort_without_remote_merge()
    test_delegation_status_writes_run_off_loop()
    test_team_ask_status_writes_run_off_loop()
    test_team_message_context_uses_lite_session_read()
    test_team_message_validation_uses_lite_session_read()
    test_session_exists_uses_index_without_cold_root_load()
    test_session_detail_reuses_migrated_root_cache()
    test_extension_plain_load_is_read_only()
    test_jsonl_cursor_persistence_uses_dedicated_executor()
    test_event_ingester_indexes_search_outside_root_lock()
    test_private_extension_reconcile_skips_current_smoked_install()
    test_pending_node_polling_uses_public_projection_cache()
    test_filtered_provider_recovery_does_not_rescan_all_runs()
    test_provider_prune_uses_shared_scandir_helper()
    test_machine_node_snapshot_reads_are_off_loop()
    test_node_snapshot_caches_static_specs()
    test_pending_approval_listing_uses_cached_projection_off_loop()
    test_project_update_counts_batch_uses_single_store_call()
    test_frontend_entrypoints_do_not_run_smoke_subprocesses()
    test_extension_list_uses_projection_cache()
    test_extension_projection_routes_cache_json_bytes()
    test_startup_reenqueue_reads_sessions_off_loop()
    test_startup_does_not_warm_unread_by_hydrating_sessions()
    test_startup_defers_requirement_and_project_match_warmers()
    test_sidebar_organization_enrichment_stays_in_summary_index()
    test_sidebar_decoration_uses_bulk_cached_state()
    test_session_discovery_reads_mode_without_deepcopy()
    test_project_aggregates_use_bulk_cached_state()
    test_sidebar_file_paths_use_cached_sessions_dir()
    test_sidebar_payload_reuses_summary_projection_cache()
    test_session_list_uses_sorted_summary_cache()
    test_session_list_pages_last_user_prompt_order_before_full_sort()
    test_session_list_waits_briefly_for_partial_summary_warm()
    test_session_list_does_not_prewarm_snapshots()
    test_session_list_warms_event_meta_off_path()
    test_session_list_reads_user_prefs_once()
    test_session_detail_has_split_perf_timers()
    test_session_hot_paths_use_dedicated_executor_with_queue_wait_metrics()
    test_stubbed_tree_build_does_not_search_tree_per_node()
    test_tree_stub_cache_key_reads_render_seq_once()
    test_event_summary_scan_reuses_full_scan_cache()
    test_message_hydration_reuses_full_scan_cache()
    test_sidebar_summary_omits_worker_refs()
    test_summary_worker_count_uses_count_projection()
    test_summary_sidecar_stat_only_for_unchanged_summary()
    test_summary_index_skips_empty_projection_scan()
    test_summary_index_validates_missing_summary_before_provider_context()
    test_summary_index_indexes_seen_sidecars_once()
    test_summary_index_cache_is_sidecar()
    test_session_store_sessions_dir_is_cached()
    test_startup_session_search_rebuild_skips_persisted_index()
    test_project_match_rebuild_skips_unchanged_session_state()
    test_stubbed_tree_cache_key_does_not_scan_message_events()
    test_stubbed_tree_cache_attaches_root_events_after_cache_copy()
    test_startup_recovery_defers_cold_runs()
    test_startup_recovery_gate_opens_before_live_integration()
    test_recovery_dispatch_skips_reconciled_runs_before_owner_read()
    test_session_fork_index_refresh_is_root_scoped()
    test_session_organization_reads_are_cached()
    test_hydration_uses_local_projection_not_extension_backend()
    test_session_event_extension_callbacks_are_worker_only()
    test_session_event_apply_event_uses_cached_hook_snapshot()
    test_requirement_tag_refresh_is_off_startup_loop()
    test_machine_nodes_readiness_check_is_off_startup_loop()
    test_sessions_route_does_not_runtime_check_machine_nodes()
    test_sessions_route_uses_cached_remote_node_sessions()
    test_connected_session_list_defers_cold_sidebar_projections()
    test_default_session_page_uses_visible_order_cache()
    test_session_organization_refresh_is_coalesced_background_work()
    test_get_session_strips_synthetic_events_off_loop()
    test_session_detail_response_bytes_are_cached()
    test_stubbed_tree_cache_covers_broad_session_loads()
    test_run_recovery_finalize_session_manager_calls_are_off_loop()
    test_run_recovery_summarizes_repeated_skip_logs()
    test_extension_backend_get_skips_body_stream()
    test_extension_backend_invoke_has_split_perf_timers()
    test_builtin_extension_core_dispatch_precedes_backend_spec_lookup()
    test_project_update_total_is_maintained_projection()
    test_extension_list_reconciliation_is_off_loop()
    test_search_sessions_response_cache_uses_metadata_version()
    test_internal_communication_worker_lookup_is_off_loop()
    print("PASS event loop blocking regressions")
