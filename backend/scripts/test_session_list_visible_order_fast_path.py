from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


import main  # noqa: E402
import session_store  # noqa: E402


class _Sid:
    def __init__(self, value: str, *, explode: bool = False):
        self.value = value
        self.explode = explode

    def __bool__(self) -> bool:
        return True

    def __str__(self) -> str:
        if self.explode:
            raise AssertionError("materialized non-page visible id")
        return self.value


def _filters() -> dict:
    return {
        "sort_by": "updated_at",
        "offset": 0,
        "limit": 2,
        "project_path": None,
        "search": None,
        "show_archived": False,
        "file_edit_mode": None,
        "folder_ids": set(),
        "tag_ids": set(),
        "provider_ids": set(),
        "model_ids": set(),
        "modes": set(),
        "sources": set(),
        "content_scores": {},
    }


def test_visible_order_page_uses_version_guarded_indexed_lookup() -> None:
    page = [{"id": "a"}, {"id": "b"}]
    with mock.patch.object(session_store, "summary_index_version", return_value=7), \
        mock.patch.object(main, "_local_visible_order_page_ids", return_value=(["a", "b"], 2)), \
        mock.patch.object(
            session_store,
            "get_indexed_session_summaries_by_ids_if_current",
            return_value=page,
        ) as indexed, \
        mock.patch.object(session_store, "get_session_summaries_by_ids") as broad:
        result, total = main._local_session_page_for_sidebar_preserving_order(**_filters())

    assert result == page
    assert total == 2
    indexed.assert_called_once_with(["a", "b"], 7)
    broad.assert_not_called()


def test_visible_order_page_falls_back_when_index_guard_misses() -> None:
    page = [{"id": "a"}]
    with mock.patch.object(session_store, "summary_index_version", return_value=7), \
        mock.patch.object(main, "_local_visible_order_page_ids", return_value=(["a"], 1)), \
        mock.patch.object(
            session_store,
            "get_indexed_session_summaries_by_ids_if_current",
            return_value=None,
        ), \
        mock.patch.object(session_store, "get_session_summaries_by_ids", return_value=page) as broad:
        result, total = main._local_session_page_for_sidebar_preserving_order(**_filters())

    assert result == page
    assert total == 1
    broad.assert_called_once_with(["a"])


def test_visible_order_page_does_not_materialize_non_page_visible_ids() -> None:
    main._local_visible_order_cache.clear()
    filters = _filters()
    filters.update({"offset": 2, "limit": 3, "project_path": "/target"})
    summaries = [
        {"id": _Sid("before-0", explode=True), "cwd": "/target"},
        {"id": _Sid("archived", explode=True), "cwd": "/target", "archived": True},
        {"id": _Sid("hidden", explode=True), "cwd": "/target", "working_mode": "prompt_engineering"},
        {"id": _Sid("other-project", explode=True), "cwd": "/other"},
        {"id": _Sid("before-1", explode=True), "cwd": "/target"},
        {"id": _Sid("page-0"), "cwd": "/target"},
        {"id": _Sid("page-1"), "cwd": "/target"},
        {"id": _Sid("page-2"), "cwd": "/target"},
        {"id": _Sid("after-0", explode=True), "cwd": "/target"},
    ]
    by_id = {str(index): summary for index, summary in enumerate(summaries)}
    page = [{"id": "page-0"}, {"id": "page-1"}, {"id": "page-2"}]

    def lookup(sid: str, version: int) -> dict | None:
        assert version == 31
        return by_id.get(sid)

    with mock.patch.object(session_store, "summary_index_version", return_value=31), \
        mock.patch.object(main.session_manager, "ordered_summary_ids", return_value=list(by_id)), \
        mock.patch.object(
            session_store,
            "get_indexed_session_summary_if_current",
            side_effect=lookup,
        ), \
        mock.patch.object(
            session_store,
            "get_indexed_session_summaries_by_ids_if_current",
            return_value=page,
        ) as indexed, \
        mock.patch.object(session_store, "get_indexed_session_summaries_by_ids") as full_lookup, \
        mock.patch.object(session_store, "get_session_summaries_by_ids") as broad:
        result, total = main._local_session_page_for_sidebar_preserving_order(**filters)

    assert result == page
    assert total == 6
    indexed.assert_called_once_with(["page-0", "page-1", "page-2"], 31)
    full_lookup.assert_not_called()
    broad.assert_not_called()


def test_indexed_lookup_returns_none_on_version_change_or_missing_id() -> None:
    with mock.patch.object(session_store, "_summary_index_lock"), \
        mock.patch.object(session_store, "_summary_index_version", 11), \
        mock.patch.object(session_store, "_summary_index", {"a": {"id": "a"}}):
        assert session_store.get_indexed_session_summaries_by_ids_if_current(["a"], 10) is None
        assert session_store.get_indexed_session_summaries_by_ids_if_current(["a", "b"], 11) is None
        assert session_store.get_indexed_session_summaries_by_ids_if_current(["a"], 11) == [{"id": "a"}]
