from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


import main  # noqa: E402
import session_store  # noqa: E402


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
        mock.patch.object(main, "_local_visible_order_ids", return_value=(["a", "b"], 2)), \
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
        mock.patch.object(main, "_local_visible_order_ids", return_value=(["a"], 1)), \
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


def test_indexed_lookup_returns_none_on_version_change_or_missing_id() -> None:
    with mock.patch.object(session_store, "_summary_index_lock"), \
        mock.patch.object(session_store, "_summary_index_version", 11), \
        mock.patch.object(session_store, "_summary_index", {"a": {"id": "a"}}):
        assert session_store.get_indexed_session_summaries_by_ids_if_current(["a"], 10) is None
        assert session_store.get_indexed_session_summaries_by_ids_if_current(["a", "b"], 11) is None
        assert session_store.get_indexed_session_summaries_by_ids_if_current(["a"], 11) == [{"id": "a"}]
