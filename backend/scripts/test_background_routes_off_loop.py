from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_async_routes_do_not_call_session_manager_locking_reads_directly() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {
        "session_manager.exists",
        "session_manager.get",
        "session_manager.get_lite",
        "session_manager._root_id_for",
        "session_manager.is_reconcile_dirty",
        "session_manager.schedule_reconcile_if_needed",
        "session_manager._lock_for_root",
    }
    violations: list[str] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.async_stack: list[str] = []

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.async_stack.append(node.name)
            self.generic_visit(node)
            self.async_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if self.async_stack:
                return
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            if self.async_stack:
                called = ast.get_source_segment(source, node.func) or ""
                if called in forbidden:
                    violations.append(f"{node.lineno}:{self.async_stack[-1]}:{called}")
            self.generic_visit(node)

    Visitor().visit(tree)
    assert violations == []


def test_background_routes_check_session_existence_off_loop() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    get_start = source.index("async def get_session_background(")
    kill_start = source.index("async def kill_session_background(")
    ask_start = source.index("@app.post(\"/api/internal/ask-propose\")", kill_start)
    background_source = source[get_start:ask_start]
    assert "await _session_exists(app_session_id)" in background_source
    assert "if not session_manager.exists(app_session_id):" not in background_source


def test_internal_schedules_checks_session_existence_off_loop() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    route_start = source.index("async def internal_schedules(")
    route_end = source.index("@app.get(\"/api/sessions/{app_session_id}/background\"", route_start)
    route_source = source[route_start:route_end]
    assert "await _session_exists(app_session_id)" in route_source
    assert "if not session_manager.exists(app_session_id):" not in route_source


def test_extension_session_field_routes_stay_off_loop() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    route_start = source.index("async def internal_session_field(")
    route_end = source.index("@app.post(\"/api/internal/mssg\")", route_start)
    route_source = source[route_start:route_end]
    assert "await _session_exists(session_id)" in route_source
    assert "await _session_lite(session_id)" in route_source
    assert "await asyncio.to_thread(\n            session_manager.apply_session_field" in route_source
    assert "session_manager.exists(session_id)" not in route_source
    assert "session_manager.get_lite(session_id)" not in route_source


def test_trace_routes_stay_off_loop() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    route_start = source.index("async def internal_trace_list(")
    route_end = source.index("@app.get(\"/api/version\")", route_start)
    route_source = source[route_start:route_end]
    assert "await asyncio.to_thread(\n            trace_collector.list_traces" in route_source
    assert "await asyncio.to_thread(trace_collector.get_trace" in route_source
    assert "await asyncio.to_thread(\n            trace_collector.search_traces" in route_source
    assert "await asyncio.to_thread(\n            trace_collector.grep_traces" in route_source
    assert "await asyncio.to_thread(\n        trace_collector.get_trace_stats" in route_source
    assert "await asyncio.to_thread(\n        trace_collector.get_latest_trace" in route_source


def test_project_routes_stay_off_loop() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    route_start = source.index("@app.get(\"/api/projects\")")
    route_end = source.index("# ── Project structure updates", route_start)
    route_source = source[route_start:route_end]
    assert "await asyncio.to_thread(_project_aggregates)" in route_source
    assert "await asyncio.to_thread(project_store.list_projects)" in route_source
    assert "await asyncio.to_thread(\n        project_store.add_project" in route_source
    assert "await asyncio.to_thread(\n        project_store.remove_project" in route_source
    assert "await asyncio.to_thread(\n        project_store.touch_project" in route_source
    assert "await asyncio.to_thread(project_mapping_store.list_mappings)" in route_source
    assert "await asyncio.to_thread(project_mapping_store.rebuild_and_save" in route_source
    assert "await asyncio.to_thread(\n        project_mapping_store.update_group" in route_source
    assert "await asyncio.to_thread(project_mapping_store.remove_group" in route_source
    assert "project_store.list_projects()" not in route_source
    assert "project_mapping_store.list_mappings()" not in route_source


def test_sessions_filter_sort_stays_off_loop() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    route_start = source.index("async def get_sessions(")
    route_end = source.index("@app.post(\"/api/sessions/search-content\")", route_start)
    route_source = source[route_start:route_end]
    helper_start = source.index("def _build_local_sessions_page_for_list(")
    helper_end = source.index("@app.get(\"/api/sessions\")", helper_start)
    helper_source = source[helper_start:helper_end]
    assert "page, total = await asyncio.to_thread(_build_local_sessions_page_for_list, **filters)" in route_source
    assert "await asyncio.to_thread(\n                _filter_sessions_for_list_preserving_order" in route_source
    assert "await asyncio.to_thread(\n                _filter_sort_sessions_for_list" in route_source
    assert "_filter_sort_sessions_for_list(" in helper_source
    assert "_local_session_summaries_for_sidebar()" in helper_source
    assert "_decorate_local_sidebar_sessions(out[offset:end], state_snapshot)" in helper_source
    assert "await asyncio.to_thread(_sidebar_state_snapshot)" in route_source
    assert "state_snapshot=state_snapshot" in route_source
    assert "_decorate_local_sidebar_sessions,\n            out[offset:end],\n            state_snapshot" in route_source
    assert "out.sort(" not in route_source


def test_delete_and_internal_session_mutations_stay_off_loop() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    delete_start = source.index("async def _delete_session_tree(")
    delete_end = source.index("async def _auto_delete_expired_sessions(", delete_start)
    delete_source = source[delete_start:delete_end]
    assert "await asyncio.to_thread(session_manager.subtree_ids, session_id)" in delete_source
    assert "await asyncio.to_thread(session_manager.delete, session_id)" in delete_source
    assert "removed_sids = session_manager.subtree_ids(session_id)" not in delete_source
    assert "ok = session_manager.delete(session_id)" not in delete_source

    auto_start = source.index("async def _auto_delete_expired_sessions(")
    auto_end = source.index("def _session_list_sort_key(", auto_start)
    auto_source = source[auto_start:auto_end]
    assert "await asyncio.to_thread(user_prefs.get_session_auto_delete_days)" in auto_source
    assert "summaries = await asyncio.to_thread(session_manager.list)" in auto_source

    create_start = source.index("async def internal_create_session(")
    create_end = source.index("@app.post(\"/api/internal/create-sub-session\")", create_start)
    create_source = source[create_start:create_end]
    assert "sess = await asyncio.to_thread(" in create_source
    assert "session_manager.create(" in create_source
    assert "sess = session_manager.create(" not in create_source

    sub_start = source.index("async def internal_create_sub_session(")
    sub_end = source.index("def _require_extension_session_ownership(", sub_start)
    sub_source = source[sub_start:sub_end]
    assert "sub = await asyncio.to_thread(" in sub_source
    assert "session_manager.create_sub_session(" in sub_source
    assert "sub = session_manager.create_sub_session(" not in sub_source

    msg_start = source.index("async def internal_session_messages_append(")
    msg_end = source.index("@app.post(\"/api/internal/session-field\")", msg_start)
    msg_source = source[msg_start:msg_end]
    for call in (
        "session_manager.append_user_msg",
        "session_manager.append_assistant_msg",
        "session_manager.update_running_content",
        "session_manager.set_streaming",
    ):
        assert "await asyncio.to_thread(" in msg_source
        assert call in msg_source


def test_provider_and_prefs_routes_stay_off_loop() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    route_start = source.index("@app.get(\"/api/providers\")")
    route_end = source.index("# ---- Shortcut responses ----", route_start)
    route_source = source[route_start:route_end]
    for call in (
        "config_store.list_providers",
        "user_prefs.get_last_models",
        "user_prefs.get_last_reasoning_efforts",
        "config_store.add_provider",
        "config_store.get_provider",
        "config_store.update_provider",
        "config_store.delete_provider",
        "config_store.set_default_provider",
        "config_store.add_custom_model_to_default",
        "config_store.get_default_provider",
        "user_prefs.get_all",
    ):
        assert f"await asyncio.to_thread({call}" in route_source or f"asyncio.to_thread({call}" in route_source


def test_project_update_and_hooks_routes_stay_off_loop() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    project_start = source.index("# ── Project structure updates")
    project_end = source.index("@app.post(\"/api/internal/provisioned-sessions\")", project_start)
    project_source = source[project_start:project_end]
    assert "await asyncio.to_thread(project_update_store.unseen_count" in project_source
    assert "project_update_store.peek_total_unseen()" in project_source
    assert "await asyncio.to_thread(project_update_store.total_unseen" in project_source
    assert "await asyncio.to_thread(project_update_store.list_unseen" in project_source
    assert "project_update_store.append(project_id, text)" in project_source
    assert "await asyncio.to_thread(\n        lambda: {" in project_source

    hooks_start = source.index("@app.get(\"/api/hooks\")")
    hooks_end = source.index("def _parse_session_timestamp", hooks_start)
    hooks_source = source[hooks_start:hooks_end]
    for call in (
        "hook_store.list_hooks",
        "hook_store.replace_hooks",
        "hook_store.upsert_hook",
        "hook_store.delete_hook",
    ):
        assert f"await asyncio.to_thread({call}" in hooks_source


def test_session_ui_mutation_routes_stay_off_loop() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    route_start = source.index("@app.post(\"/api/sessions/{session_id}/fork\")")
    route_end = source.index("@app.post(\"/api/sessions/{session_id}/project-suggestion\")", route_start)
    route_source = source[route_start:route_end]
    for call in (
        "session_manager.fork",
        "session_manager.set_fork_closed",
        "session_manager.rename",
        "session_manager.set_pinned",
        "session_manager.set_worker_eligible",
        "session_manager.set_worker_creation_policy",
        "session_manager.set_archived",
        "session_manager.set_selectors",
    ):
        assert (
            f"asyncio.to_thread({call}" in route_source
            or f"asyncio.to_thread(\n        {call}" in route_source
    )
    assert "session_manager.get_ref(session_id)" not in route_source

    panel_start = source.index("@app.post(\"/api/sessions/{session_id}/tags\")")
    panel_end = source.index("def _require_prompt_engineer_internal", panel_start)
    panel_source = source[panel_start:panel_end]
    assert "_require_session(session_id)" not in panel_source
    for call in (
        "session_manager.add_tag",
        "session_manager.update_tag",
        "session_manager.remove_tag",
        "session_manager.clear_tags",
        "session_manager.add_note",
        "session_manager.remove_note",
        "session_manager.update_note",
        "session_manager.set_right_panel",
        "session_manager.add_open_file_panel",
        "session_manager.remove_open_file_panel",
        "session_manager.set_open_file_panels",
        "session_manager.add_open_config_panel",
        "session_manager.remove_open_config_panel",
        "session_manager.set_open_config_panels",
    ):
        assert f"asyncio.to_thread(\n        {call}" in panel_source or f"asyncio.to_thread({call}" in panel_source


def test_core_create_session_validation_stays_off_loop() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    route_start = source.index("async def create_session(")
    route_end = source.index("@app.post(\"/api/sessions/{session_id}/fork\")", route_start)
    route_source = source[route_start:route_end]
    assert "await asyncio.to_thread(\n        _provider_for_required_model" in route_source
    assert "await asyncio.to_thread(\n        _provider_reasoning_effort" in route_source
    assert "await asyncio.to_thread(config_store.apply_env_vars)" in route_source


if __name__ == "__main__":
    test_async_routes_do_not_call_session_manager_locking_reads_directly()
    test_background_routes_check_session_existence_off_loop()
    test_internal_schedules_checks_session_existence_off_loop()
    test_extension_session_field_routes_stay_off_loop()
    test_trace_routes_stay_off_loop()
    test_project_routes_stay_off_loop()
    test_sessions_filter_sort_stays_off_loop()
    test_delete_and_internal_session_mutations_stay_off_loop()
    test_provider_and_prefs_routes_stay_off_loop()
    test_project_update_and_hooks_routes_stay_off_loop()
    test_session_ui_mutation_routes_stay_off_loop()
    test_core_create_session_validation_stays_off_loop()
    print("PASS background routes stay off event loop")
