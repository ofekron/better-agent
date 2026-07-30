from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).parents[1]

# Every module that registers HTTP/WS routes. main.py is the composition root
# and declares none of its own, so the route invariants below scan the owners.
API_MODULES = (
    "main.py",
    "ws_chat.py",
    "providers_api.py",
    "user_prefs_api.py",
    "projects_api.py",
    "hooks_push_api.py",
    "harness_profiles_api.py",
    "internal_extension_api.py",
    "session_listing_api.py",
    "session_list_cache.py",
    "session_detail_api.py",
    "session_panels_api.py",
    "file_editor_api.py",
    "offline_actions_api.py",
    "recovery.py",
    "internal_orchestration_api.py",
    "internal_session_state_api.py",
    "internal_messaging_api.py",
    "user_input_api.py",
    "tasks_api.py",
    "session_bridge_api.py",
    "schedules_api.py",
    "coordination_api.py",
    "mobile_desktop_api.py",
    "workers_api.py",
    "worker_pools_api.py",
    "credential_ui_api.py",
    "tool_approvals_api.py",
    "ops_api.py",
    "misc_api.py",
)


def _offloaded(source: str, call: str) -> bool:
    """True when `call` is handed to asyncio.to_thread, regardless of how the
    argument list is wrapped across lines."""
    return re.search(
        r"asyncio\.to_thread\(\s*" + re.escape(call) + r"\b", source,
    ) is not None


def _dotted_name(node: ast.expr) -> str | None:
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def test_async_routes_do_not_call_session_manager_locking_reads_directly() -> None:
    forbidden = {
        "session_manager.exists",
        "session_manager.get",
        "session_manager.get_lite",
        "session_manager._root_id_for",
        "session_manager._lock_for_root",
    }
    violations: list[str] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self, module: str) -> None:
            self.module = module
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
                called = _dotted_name(node.func)
                if called in forbidden:
                    violations.append(
                        f"{self.module}:{node.lineno}:{self.async_stack[-1]}:{called}"
                    )
            self.generic_visit(node)

    for module in API_MODULES:
        path = ROOT / module
        assert path.exists(), f"{module} is missing; update API_MODULES"
        Visitor(module).visit(ast.parse(path.read_text(encoding="utf-8")))
    assert violations == []


def test_internal_schedules_checks_session_existence_off_loop() -> None:
    source = (ROOT / "schedules_api.py").read_text(encoding="utf-8")
    route_start = source.index("async def internal_schedules(")
    route_end = source.index("@router.get(\"/api/schedules\")", route_start)
    route_source = source[route_start:route_end]
    assert "await _session_exists(app_session_id)" in route_source
    assert "if not session_manager.exists(app_session_id):" not in route_source


def test_extension_session_field_routes_stay_off_loop() -> None:
    source = (ROOT / "internal_session_state_api.py").read_text(encoding="utf-8")
    route_start = source.index("async def internal_session_field(")
    route_end = source.index("def _session_activity_snapshot(", route_start)
    route_source = source[route_start:route_end]
    assert "await _session_exists(session_id)" in route_source
    assert "await _session_lite(session_id)" in route_source
    assert "await asyncio.to_thread(\n            session_manager.apply_session_field" in route_source
    assert "session_manager.exists(session_id)" not in route_source
    assert "session_manager.get_lite(session_id)" not in route_source


def test_project_routes_stay_off_loop() -> None:
    route_source = (ROOT / "projects_api.py").read_text(encoding="utf-8")
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
    source = (ROOT / "session_listing_api.py").read_text(encoding="utf-8")
    route_start = source.index("async def get_sessions(")
    route_end = source.index("@router.post(\"/api/sessions/search-content\")", route_start)
    route_source = source[route_start:route_end]
    helper_start = source.index("def _build_local_sessions_page_for_list(")
    helper_end = source.index("@router.get(\"/api/sessions\")", helper_start)
    helper_source = source[helper_start:helper_end]
    assert "page, total = await session_list_path.run(\n            \"sessions.list.local_page_thread\"," in route_source
    assert "_build_local_sessions_page_for_list," in route_source
    assert "await asyncio.to_thread(\n                _filter_sessions_for_list_preserving_order" in route_source
    assert "await asyncio.to_thread(\n                _filter_sort_sessions_for_list" in route_source
    assert "_filter_sort_sessions_for_list(" in helper_source
    assert "_local_session_summaries_for_sidebar()" in helper_source
    assert "_decorate_local_sidebar_sessions(out[offset:end], state_snapshot)" in helper_source
    assert "await asyncio.to_thread(_sidebar_state_snapshot)" in route_source
    assert "state_snapshot=state_snapshot" in route_source
    assert "_decorate_local_sidebar_sessions,\n            page_source,\n            state_snapshot" in route_source
    assert "out.sort(" not in route_source


def test_delete_and_internal_session_mutations_stay_off_loop() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    delete_start = source.index("async def _delete_session_tree(")
    delete_source = source[delete_start:]
    assert "await asyncio.to_thread(session_manager.subtree_ids, session_id)" in delete_source
    assert "await asyncio.to_thread(session_manager.delete, session_id)" in delete_source
    assert "removed_sids = session_manager.subtree_ids(session_id)" not in delete_source
    assert "ok = session_manager.delete(session_id)" not in delete_source

    recovery_source = (ROOT / "recovery.py").read_text(encoding="utf-8")
    auto_start = recovery_source.index("async def _auto_delete_expired_sessions(")
    auto_end = recovery_source.index(
        "async def _reconcile_queued_delivery_attempt(", auto_start,
    )
    auto_source = recovery_source[auto_start:auto_end]
    assert "await asyncio.to_thread(user_prefs.get_session_auto_delete_days)" in auto_source
    assert "summaries = await asyncio.to_thread(session_manager.list)" in auto_source

    orchestration = (ROOT / "internal_orchestration_api.py").read_text(encoding="utf-8")
    create_start = orchestration.index("async def internal_create_session(")
    create_end = orchestration.index("@router.post(\"/api/internal/create-sub-session\")", create_start)
    create_source = orchestration[create_start:create_end]
    assert "sess = await asyncio.to_thread(" in create_source
    assert "session_manager.create(" in create_source
    assert "sess = session_manager.create(" not in create_source

    sub_start = orchestration.index("async def internal_create_sub_session(")
    sub_source = orchestration[sub_start:]
    assert "sub = await asyncio.to_thread(" in sub_source
    assert "session_manager.create_sub_session(" in sub_source
    assert "sub = session_manager.create_sub_session(" not in sub_source

    session_state = (ROOT / "internal_session_state_api.py").read_text(encoding="utf-8")
    msg_start = session_state.index("async def internal_session_messages_append(")
    msg_end = session_state.index("@router.post(\"/api/internal/session-field\")", msg_start)
    msg_source = session_state[msg_start:msg_end]
    for call in (
        "session_manager.append_user_msg",
        "session_manager.append_assistant_msg",
        "session_manager.update_running_content",
        "session_manager.set_streaming",
    ):
        assert "await asyncio.to_thread(" in msg_source
        assert call in msg_source


def test_provider_and_prefs_routes_stay_off_loop() -> None:
    prefs_source = (ROOT / "user_prefs_api.py").read_text(encoding="utf-8")
    prefs_source = prefs_source[: prefs_source.index("# ---- Shortcut responses ----")]
    route_source = (
        (ROOT / "providers_api.py").read_text(encoding="utf-8")
        + (ROOT / "runtime_profiles_api.py").read_text(encoding="utf-8")
        + prefs_source
    )
    for call in (
        "config_store.list_providers",
        "user_prefs.get_last_models",
        "user_prefs.get_last_reasoning_efforts",
        "config_store.add_provider",
        "config_store.get_provider",
        "config_store.update_provider",
        "config_store.delete_provider",
        "config_store.add_runtime_profile",
        "config_store.update_runtime_profile",
        "config_store.delete_runtime_profile",
        "config_store.activate_runtime_profile",
        "config_store.add_custom_model_to_default",
        "config_store.get_default_provider",
        "user_prefs.get_all",
    ):
        assert _offloaded(route_source, call)


def test_project_update_and_hooks_routes_stay_off_loop() -> None:
    hooks = (ROOT / "hooks_push_api.py").read_text(encoding="utf-8")
    project_source = (ROOT / "internal_extension_api.py").read_text(encoding="utf-8")
    structure_gate = "await _require_project_structure_internal_async(request, x_internal_token)"
    updates_gate = "await require_project_updates_internal_async(request, x_internal_token)"
    assert "async def _require_project_structure_internal_async" in hooks
    assert "internal_guards.require_builtin_runtime_extension(" in hooks
    assert "asyncio.to_thread(_require_project_structure_internal, x_internal_token)" not in hooks
    assert hooks.count(structure_gate) + project_source.count(updates_gate) >= 9
    for route in (
        "internal_project_update_count",
        "internal_project_update_total",
        "internal_project_update_counts_batch",
        "internal_project_updates_unseen",
        "capture_project_update",
        "internal_project_updates_list",
        "internal_project_updates_mark_seen",
    ):
        route_start = project_source.index(f"async def {route}(")
        route_end = project_source.index("@router.", route_start + 1)
        route_source = project_source[route_start:route_end]
        assert updates_gate in route_source
        assert "await asyncio.to_thread(_require_project" not in route_source
    assert "await asyncio.to_thread(project_update_store.unseen_count" in project_source
    assert "project_update_store.peek_total_unseen()" in project_source
    assert "await asyncio.to_thread(project_update_store.total_unseen" in project_source
    assert "await asyncio.to_thread(project_update_store.list_unseen" in project_source
    assert "project_update_store.append(project_id, text)" in project_source
    for route, calls in {
        "capture_project_update": (
            "project_update_store.append(project_id, text)",
            "project_update_store.unseen_count(project_id)",
        ),
        "internal_project_updates_list": (
            "project_update_store.unseen_count(project_id)",
            "project_update_store.list_unseen(project_id)",
        ),
        "internal_project_updates_mark_seen": (
            "project_update_store.mark_seen(project_id, entry_ids)",
            "project_update_store.unseen_count(project_id)",
        ),
    }.items():
        route_start = project_source.index(f"async def {route}(")
        route_end = project_source.index("@router.", route_start + 1)
        route_source = project_source[route_start:route_end]
        assert "await asyncio.to_thread(\n        lambda: (" in route_source
        for call in calls:
            assert call in route_source

    for call in (
        "hook_store.list_hooks",
        "hook_store.replace_hooks",
        "hook_store.upsert_hook",
        "hook_store.delete_hook",
    ):
        assert _offloaded(hooks, call)


def test_session_ui_mutation_routes_stay_off_loop() -> None:
    source = (ROOT / "session_detail_api.py").read_text(encoding="utf-8")
    route_start = source.index("@router.post(\"/api/sessions/{session_id}/fork\")")
    route_end = source.index("@router.post(\"/api/sessions/{session_id}/project-suggestion\")", route_start)
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
        assert _offloaded(route_source, call)
    assert "session_manager.get_ref(session_id)" not in route_source

    panels = (ROOT / "session_panels_api.py").read_text(encoding="utf-8")
    panel_source = panels[panels.index("@router.post(\"/api/sessions/{session_id}/tags\")"):]
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
        assert _offloaded(panel_source, call)


def test_core_create_session_validation_stays_off_loop() -> None:
    source = (ROOT / "session_detail_api.py").read_text(encoding="utf-8")
    route_start = source.index("async def create_session(")
    route_end = source.index("@router.post(\"/api/sessions/{session_id}/fork\")", route_start)
    route_source = source[route_start:route_end]
    assert "await asyncio.to_thread(\n        _provider_for_required_model" in route_source
    assert "await asyncio.to_thread(\n        _provider_reasoning_effort" in route_source
    assert "await asyncio.to_thread(config_store.apply_env_vars)" in route_source


if __name__ == "__main__":
    test_async_routes_do_not_call_session_manager_locking_reads_directly()
    test_internal_schedules_checks_session_existence_off_loop()
    test_extension_session_field_routes_stay_off_loop()
    test_project_routes_stay_off_loop()
    test_sessions_filter_sort_stays_off_loop()
    test_delete_and_internal_session_mutations_stay_off_loop()
    test_provider_and_prefs_routes_stay_off_loop()
    test_project_update_and_hooks_routes_stay_off_loop()
    test_session_ui_mutation_routes_stay_off_loop()
    test_core_create_session_validation_stays_off_loop()
    print("PASS background routes stay off event loop")
