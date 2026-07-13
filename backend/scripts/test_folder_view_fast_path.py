"""Regression: enabling folder_view must not reorder sessions that have no
folder assignment, and must not force the sidebar list off the fast,
order-preserving pagination path.

Before this fix, `folder_view=True` unconditionally disabled the
incrementally-maintained summary order index (`_can_preserve_summary_order`,
`_can_page_local_summary_order`, `_can_page_default_updated_at_with_virtual`
all had an `and not folder_view` clause), forcing a fallback to a raw
unsorted enumeration re-sorted with a DIFFERENT tie-break for sessions tied
on (isEmpty, pinned, updated_at). That produced a visible reorder in the
sidebar even with zero foldered sessions.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_folder_view_fast_path.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

HOME = tempfile.TemporaryDirectory(prefix="better-agent-folder-view-fast-path-")
os.environ["BETTER_AGENT_HOME"] = HOME.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
import session_store  # noqa: E402


def _summary(id_: str, *, updated_at: str, folder_id: str | None = None) -> dict:
    return {
        "id": id_,
        "folder_id": folder_id,
        "updated_at": updated_at,
        "last_user_prompt_at": None,
        "last_opened_at": None,
        "pinned": False,
        "archived": False,
        "working_mode": None,
        "working_mode_meta": None,
        "cwd": None,
        "all_projects": False,
    }


def _seed(summaries: list[dict]) -> None:
    with session_store._summary_index_lock:
        session_store._summary_index.clear()
        session_store._summary_index.update((s["id"], s) for s in summaries)
        session_store._summary_index_loaded = True
        session_store._summary_index_version += 1
        session_store._summary_order_version += 1
        session_store._summary_visibility_version += 1
        session_store._summary_sorted_id_caches.clear()
        session_store._sidebar_page_projections.clear()


def _page_ids(folder_view: bool) -> list[str]:
    page, _total, _order_gen, _vis_gen = session_store.sidebar_session_summary_page(
        "updated_at", None, 0, 10, folder_view=folder_view,
    )
    return [s["id"] for s in page]


def test_no_foldered_sessions_folder_view_toggle_is_a_true_no_op() -> None:
    _seed([_summary(f"tied-{i}", updated_at="2026-06-16T00:00:00") for i in range(5)])
    assert _page_ids(folder_view=False) == _page_ids(folder_view=True)


def test_fast_path_groups_folderized_sessions_first_when_folder_view_enabled() -> None:
    _seed([
        _summary("folderized-old", updated_at="2026-05-01T00:00:00", folder_id="folder-1"),
        _summary("unfiled-new", updated_at="2026-06-16T00:00:00"),
    ])
    assert _page_ids(folder_view=True) == ["folderized-old", "unfiled-new"]
    assert _page_ids(folder_view=False) == ["unfiled-new", "folderized-old"]


def test_folder_reassignment_invalidates_the_cached_fast_path_order() -> None:
    _seed([
        _summary("a", updated_at="2026-06-16T00:00:00"),
        _summary("b", updated_at="2026-06-15T00:00:00"),
    ])
    assert _page_ids(folder_view=True) == ["a", "b"]

    with session_store._summary_index_lock:
        existing = session_store._summary_index["b"]
        updated = dict(existing, folder_id="folder-1")
        if session_store._summary_order_changed(existing, updated):
            session_store._summary_order_version += 1
        session_store._summary_index["b"] = updated

    assert _page_ids(folder_view=True) == ["b", "a"]


def test_fast_path_gates_no_longer_exclude_folder_view() -> None:
    assert main._can_preserve_summary_order(
        search_query="",
        appended_virtual_sessions=False,
        sort_by="updated_at",
        status_sort=False,
    )
    assert main._can_page_local_summary_order(
        search_query="",
        sort_by="updated_at",
        status_sort=False,
    )
    assert main._can_page_default_updated_at_with_virtual(
        search_query="",
        project_path=None,
        show_archived=False,
        file_edit_mode=None,
        folder_ids=set(),
        tag_ids=set(),
        provider_ids=set(),
        model_ids=set(),
        modes=set(),
        sources=set(),
        sort_by="updated_at",
        status_sort=False,
    )


def test_local_page_for_sidebar_preserving_order_forwards_folder_view() -> None:
    with mock.patch.object(
        session_store,
        "sidebar_session_summary_page",
        return_value=([{"id": "a"}], 1, 1, 1),
    ) as fast_path:
        page, total = main._local_session_page_for_sidebar_preserving_order(
            sort_by="updated_at",
            offset=0,
            limit=10,
            project_path=None,
            search=None,
            show_archived=False,
            file_edit_mode=None,
            folder_ids=set(),
            tag_ids=set(),
            provider_ids=set(),
            model_ids=set(),
            modes=set(),
            sources=set(),
            content_scores={},
            folder_view=True,
        )

    assert page == [{"id": "a"}]
    assert total == 1
    fast_path.assert_called_once_with("updated_at", None, 0, 10, folder_view=True)


if __name__ == "__main__":
    try:
        test_no_foldered_sessions_folder_view_toggle_is_a_true_no_op()
        test_fast_path_groups_folderized_sessions_first_when_folder_view_enabled()
        test_folder_reassignment_invalidates_the_cached_fast_path_order()
        test_fast_path_gates_no_longer_exclude_folder_view()
        test_local_page_for_sidebar_preserving_order_forwards_folder_view()
        print("PASS: folder view fast path")
    finally:
        HOME.cleanup()
