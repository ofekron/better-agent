from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
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
    ordered_ids = [
        "before-0", "archived", "hidden", "other-project", "before-1",
        "page-0", "page-1", "page-2", "after-0",
    ]
    by_id = dict(zip(ordered_ids, summaries, strict=True))
    page = [{"id": "page-0"}, {"id": "page-1"}, {"id": "page-2"}]

    def lookup(sid: str, version: int) -> dict | None:
        assert version == 31
        return by_id.get(sid)

    with mock.patch.object(session_store, "summary_index_version", return_value=31), \
        mock.patch.object(main.session_manager, "ordered_summary_ids", return_value=ordered_ids), \
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


def test_concurrent_visible_order_pages_share_one_large_projection_build() -> None:
    main._local_visible_order_cache.clear()
    main._local_visible_order_inflight.clear()
    row_count = 20_000
    ordered_ids = [f"session-{index}" for index in range(row_count)]
    summaries = {sid: {"id": sid} for sid in ordered_ids}
    lookup_count = 0
    lookup_lock = threading.Lock()
    start = threading.Barrier(4)

    def lookup(sid: str) -> dict:
        nonlocal lookup_count
        with lookup_lock:
            lookup_count += 1
        if lookup_count == 1:
            time.sleep(0.03)
        return summaries[sid]

    def get_page(offset: int) -> tuple[list[str], int] | None:
        start.wait()
        return main._local_visible_order_page_ids(
            "updated_at", None, offset, 50, 17, 23,
        )

    started = time.perf_counter()
    with mock.patch.object(main.session_manager, "ordered_summary_ids", return_value=ordered_ids) as ordered, \
        mock.patch.object(session_store, "get_indexed_session_summary_if_current", side_effect=lambda sid, version: lookup(sid)), \
        mock.patch.object(session_store, "summary_index_version", return_value=17), \
        mock.patch.object(session_store, "summary_order_version", return_value=23), \
        ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(get_page, (0, 50, 100, 150)))
    elapsed = time.perf_counter() - started

    assert ordered.call_count == 1
    assert lookup_count == row_count
    assert elapsed < 2.0
    for index, result in enumerate(results):
        assert result == (ordered_ids[index * 50:index * 50 + 50], row_count)


def test_oversized_visible_order_resolves_waiters_without_being_cached() -> None:
    main._local_visible_order_cache.clear()
    main._local_visible_order_inflight.clear()
    row_count = main._LOCAL_VISIBLE_ORDER_CACHE_MAX_IDS + 1
    ordered_ids = [f"session-{index}" for index in range(row_count)]
    build_count = 0
    build_lock = threading.Lock()

    def ordered(sort_by: str) -> list[str]:
        nonlocal build_count
        assert sort_by == "updated_at"
        with build_lock:
            build_count += 1
        time.sleep(0.03)
        return ordered_ids

    def lookup(sid: str, version: int) -> dict:
        assert version == 41
        return {"id": sid}

    def run_wave() -> list[tuple[list[str], int] | None]:
        start = threading.Barrier(3)

        def get_page(offset: int) -> tuple[list[str], int] | None:
            start.wait()
            return main._local_visible_order_page_ids(
                "updated_at", None, offset, 2, 41, 43,
            )

        with ThreadPoolExecutor(max_workers=3) as executor:
            return list(executor.map(get_page, (0, 2, 4)))

    with mock.patch.object(main.session_manager, "ordered_summary_ids", side_effect=ordered), \
        mock.patch.object(session_store, "get_indexed_session_summary_if_current", side_effect=lookup), \
        mock.patch.object(session_store, "summary_index_version", return_value=41), \
        mock.patch.object(session_store, "summary_order_version", return_value=43), \
        mock.patch.object(main.perf, "record") as record:
        first = run_wave()
        assert build_count == 1
        assert not main._local_visible_order_cache
        assert not main._local_visible_order_inflight
        second = run_wave()

    expected = [
        (ordered_ids[0:2], row_count),
        (ordered_ids[2:4], row_count),
        (ordered_ids[4:6], row_count),
    ]
    assert first == expected
    assert second == expected
    assert build_count == 2
    assert not main._local_visible_order_cache
    assert not main._local_visible_order_inflight
    assert sum(map(len, main._local_visible_order_cache.values())) <= main._LOCAL_VISIBLE_ORDER_CACHE_MAX_IDS
    assert record.call_args_list.count(
        mock.call("sessions.list.local.visible_order_projection.ids", float(row_count))
    ) == 2
    assert record.call_args_list.count(
        mock.call("sessions.list.local.visible_order_cache.oversize_bypass", 1.0)
    ) == 2


def test_visible_order_stale_generation_is_not_published() -> None:
    main._local_visible_order_cache.clear()
    main._local_visible_order_inflight.clear()
    index_version = 7

    def lookup(sid: str) -> dict:
        nonlocal index_version
        index_version = 8
        return {"id": sid}

    with mock.patch.object(main.session_manager, "ordered_summary_ids", return_value=["a"]), \
        mock.patch.object(session_store, "get_indexed_session_summary_if_current", side_effect=lambda sid, version: lookup(sid)), \
        mock.patch.object(session_store, "summary_index_version", side_effect=lambda: index_version), \
        mock.patch.object(session_store, "summary_order_version", return_value=11):
        result = main._local_visible_order_page_ids("updated_at", None, 0, 50, 7, 11)

    assert result is None
    assert not main._local_visible_order_cache
    assert not main._local_visible_order_inflight


def test_visible_order_build_failure_releases_all_waiters_and_allows_retry() -> None:
    main._local_visible_order_cache.clear()
    main._local_visible_order_inflight.clear()
    start = threading.Barrier(3)
    first_lookup = threading.Event()
    release_failure = threading.Event()

    def failing_lookup(sid: str) -> dict:
        first_lookup.set()
        release_failure.wait(timeout=1)
        raise RuntimeError("projection failed")

    def get_page() -> tuple[list[str], int] | None:
        start.wait()
        return main._local_visible_order_page_ids("updated_at", None, 0, 1, 3, 5)

    with mock.patch.object(main.session_manager, "ordered_summary_ids", return_value=["a"]), \
        mock.patch.object(session_store, "get_indexed_session_summary_if_current", side_effect=lambda sid, version: failing_lookup(sid)), \
        ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(get_page) for _ in range(3)]
        assert first_lookup.wait(timeout=1)
        release_failure.set()
        for future in futures:
            try:
                future.result(timeout=1)
            except RuntimeError as exc:
                assert str(exc) == "projection failed"
            else:
                raise AssertionError("projection failure did not reach waiter")

    assert not main._local_visible_order_inflight
    with mock.patch.object(main.session_manager, "ordered_summary_ids", return_value=["a"]), \
        mock.patch.object(session_store, "get_indexed_session_summary_if_current", return_value={"id": "a"}), \
        mock.patch.object(session_store, "summary_index_version", return_value=3), \
        mock.patch.object(session_store, "summary_order_version", return_value=5):
        assert main._local_visible_order_page_ids("updated_at", None, 0, 1, 3, 5) == (["a"], 1)


class _PausedCompletionFuture(Future):
    def __init__(self, completion: str) -> None:
        super().__init__()
        self._completion = completion
        self.completion_entered = threading.Event()
        self.waiter_entered = threading.Event()
        self.release_completion = threading.Event()

    def result(self, timeout: float | None = None):
        self.waiter_entered.set()
        return super().result(timeout)

    def set_result(self, result) -> None:
        if self._completion == "result":
            self.completion_entered.set()
            assert self.release_completion.wait(timeout=1)
        super().set_result(result)

    def set_exception(self, exception) -> None:
        if self._completion == "exception":
            self.completion_entered.set()
            assert self.release_completion.wait(timeout=1)
        super().set_exception(exception)


def test_visible_order_result_completion_stays_singleflight_until_terminal() -> None:
    main._local_visible_order_cache.clear()
    main._local_visible_order_inflight.clear()
    publication = _PausedCompletionFuture("result")
    build_count = 0

    def ordered(sort_by: str) -> list[str]:
        nonlocal build_count
        build_count += 1
        return ["a"]

    with mock.patch.object(main, "Future", return_value=publication), \
        mock.patch.object(main, "_LOCAL_VISIBLE_ORDER_CACHE_MAX_IDS", 0), \
        mock.patch.object(main.session_manager, "ordered_summary_ids", side_effect=ordered), \
        mock.patch.object(session_store, "get_indexed_session_summary_if_current", return_value={"id": "a"}), \
        mock.patch.object(session_store, "summary_index_version", return_value=47), \
        mock.patch.object(session_store, "summary_order_version", return_value=53), \
        ThreadPoolExecutor(max_workers=2) as executor:
        builder = executor.submit(
            main._local_visible_order_page_ids, "updated_at", None, 0, 1, 47, 53,
        )
        assert publication.completion_entered.wait(timeout=1)
        joiner = executor.submit(
            main._local_visible_order_page_ids, "updated_at", None, 0, 1, 47, 53,
        )
        assert publication.waiter_entered.wait(timeout=1)
        assert build_count == 1
        publication.release_completion.set()
        assert builder.result(timeout=1) == (["a"], 1)
        assert joiner.result(timeout=1) == (["a"], 1)

    assert build_count == 1
    assert not main._local_visible_order_inflight


def test_visible_order_exception_completion_stays_singleflight_and_retries() -> None:
    main._local_visible_order_cache.clear()
    main._local_visible_order_inflight.clear()
    publication = _PausedCompletionFuture("exception")
    build_count = 0

    def ordered(sort_by: str) -> list[str]:
        nonlocal build_count
        build_count += 1
        return ["a"]

    with mock.patch.object(main, "Future", return_value=publication), \
        mock.patch.object(main.session_manager, "ordered_summary_ids", side_effect=ordered), \
        mock.patch.object(
            session_store,
            "get_indexed_session_summary_if_current",
            side_effect=RuntimeError("projection failed"),
        ), \
        ThreadPoolExecutor(max_workers=2) as executor:
        builder = executor.submit(
            main._local_visible_order_page_ids, "updated_at", None, 0, 1, 59, 61,
        )
        assert publication.completion_entered.wait(timeout=1)
        joiner = executor.submit(
            main._local_visible_order_page_ids, "updated_at", None, 0, 1, 59, 61,
        )
        assert publication.waiter_entered.wait(timeout=1)
        assert build_count == 1
        publication.release_completion.set()
        for request in (builder, joiner):
            try:
                request.result(timeout=1)
            except RuntimeError as exc:
                assert str(exc) == "projection failed"
            else:
                raise AssertionError("projection failure did not reach caller")

    assert build_count == 1
    assert not main._local_visible_order_inflight
    with mock.patch.object(main.session_manager, "ordered_summary_ids", return_value=["a"]), \
        mock.patch.object(session_store, "get_indexed_session_summary_if_current", return_value={"id": "a"}), \
        mock.patch.object(session_store, "summary_index_version", return_value=59), \
        mock.patch.object(session_store, "summary_order_version", return_value=61):
        assert main._local_visible_order_page_ids(
            "updated_at", None, 0, 1, 59, 61,
        ) == (["a"], 1)
