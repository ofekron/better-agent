"""Regression test for /api/sessions/search-content.

Run with:
    cd backend && .venv/bin/python scripts/test_search_content_endpoint.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-search-content-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import auth  # noqa: E402
from event_journal import publish_event_sync  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
import session_search_index  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
HEADERS = {"Authorization": f"Bearer {auth.create_token('test')}"}


def main_test() -> bool:
    content_sess = session_manager.create(
        name="content-session",
        model="m",
        cwd="/tmp",
        orchestration_mode="native",
    )
    content_sid = content_sess["id"]
    publish_event_sync(
        session_id=content_sid,
        context_id=content_sid,
        event_type="agent_message",
        data={
            "uuid": "search-event-1",
            "type": "assistant",
            "message": {"content": "content-only-needle"},
        },
        source="test",
        timeout=5,
    )
    title_sess = session_manager.create(
        name="title-only-needle",
        model="m",
        cwd="/tmp",
        orchestration_mode="native",
    )
    prompt_sess = session_manager.create(
        name="prompt-session",
        model="m",
        cwd="/tmp",
        orchestration_mode="native",
    )
    session_manager.append_user_msg(
        prompt_sess["id"],
        {
            "id": "first-prompt-msg",
            "role": "user",
            "content": "first-prompt-only-needle",
            "timestamp": "2026-01-01T00:00:00Z",
        },
    )
    session_search_index._drain_pending()
    with TestClient(main.app, client=("127.0.0.1", 50000)) as client:
        content_response = client.post(
            "/api/sessions/search-content",
            json={"query": "content-only-needle", "limit": 5, "fields": ["content"]},
            headers=HEADERS,
        )
        title_response = client.post(
            "/api/sessions/search-content",
            json={"query": "title-only-needle", "limit": 5, "fields": ["title"]},
            headers=HEADERS,
        )
        prompt_response = client.post(
            "/api/sessions/search-content",
            json={"query": "first-prompt-only-needle", "limit": 5, "fields": ["first_prompt"]},
            headers=HEADERS,
        )
        excluded_response = client.post(
            "/api/sessions/search-content",
            json={"query": "title-only-needle", "limit": 5, "fields": ["content"]},
            headers=HEADERS,
        )
        list_response = None
        list_sessions = []
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            list_response = client.get(
                "/api/sessions?search=content-only-needle&search_fields=content",
                headers=HEADERS,
            )
            list_sessions = list_response.json().get("sessions") if list_response.status_code == 200 else []
            if list_sessions:
                break
            time.sleep(0.05)
    content_results = content_response.json().get("results") if content_response.status_code == 200 else []
    title_results = title_response.json().get("results") if title_response.status_code == 200 else []
    prompt_results = prompt_response.json().get("results") if prompt_response.status_code == 200 else []
    excluded_results = excluded_response.json().get("results") if excluded_response.status_code == 200 else []
    if list_response is None:
        raise AssertionError("list response was not attempted")
    ok = (
        content_response.status_code == 200
        and title_response.status_code == 200
        and prompt_response.status_code == 200
        and excluded_response.status_code == 200
        and list_response.status_code == 200
        and isinstance(content_results, list)
        and isinstance(title_results, list)
        and isinstance(prompt_results, list)
        and isinstance(list_sessions, list)
        and content_results
        and title_results
        and prompt_results
        and list_sessions
        and content_results[0].get("session_id") == content_sid
        and title_results[0].get("session_id") == title_sess["id"]
        and prompt_results[0].get("session_id") == prompt_sess["id"]
        and list_sessions[0].get("id") == content_sid
        and list_sessions[0].get("search_score", 0) > 0
        and excluded_results == []
    )
    print(
        f"{PASS if ok else FAIL} search-content honors selected fields "
        f"-- content={content_response.text[:120]!r} title={title_response.text[:120]!r} "
        f"prompt={prompt_response.text[:120]!r} excluded={excluded_response.text[:120]!r}",
    )
    return ok


def index_event_nonblocking_test() -> bool:
    original_apply = session_search_index._apply_rows

    def slow_apply(rows):
        time.sleep(0.25)
        return original_apply(rows)

    session_search_index._apply_rows = slow_apply
    started = time.monotonic()
    try:
        session_search_index.index_event(
            "sid-nonblocking",
            {"data": {"message": "slow index write should not block ingest"}},
        )
        elapsed = time.monotonic() - started
        session_search_index._drain_pending()
    finally:
        session_search_index._apply_rows = original_apply
    ok = elapsed < 0.05
    print(
        f"{PASS if ok else FAIL} search index event enqueue is nonblocking "
        f"-- elapsed={elapsed:.3f}s",
    )
    return ok


def search_does_not_wait_for_pending_index_test() -> bool:
    original_apply = session_search_index._apply_rows

    def slow_apply(rows):
        time.sleep(0.25)
        return original_apply(rows)

    session_search_index._apply_rows = slow_apply
    try:
        session_search_index.index_event(
            "sid-pending-search",
            {"data": {"message": "pending search index should not block search"}},
        )
        started = time.monotonic()
        session_search_index.search("unlikely-pending-search-query", limit=5)
        elapsed = time.monotonic() - started
        session_search_index._drain_pending()
    finally:
        session_search_index._apply_rows = original_apply
    ok = elapsed < 0.05
    print(
        f"{PASS if ok else FAIL} search does not wait for pending index "
        f"-- elapsed={elapsed:.3f}s",
    )
    return ok


def grep_sessions_passes_bounded_limit_test() -> bool:
    import session_store

    seen: list[int] = []
    original_search = session_search_index.search

    def fake_search(query: str, limit: int = 50, *, max_wait_seconds=None):
        seen.append(limit)
        return []

    session_search_index.search = fake_search
    try:
        session_store.grep_sessions("needle", limit=7, fields=["content"])
    finally:
        session_search_index.search = original_search
    ok = seen == [7]
    print(f"{PASS if ok else FAIL} grep_sessions bounds content index query -- limits={seen}")
    return ok


def bounded_search_returns_while_cache_fills_test() -> bool:
    session_search_index._search_cache.clear()
    session_search_index._search_inflight.clear()
    original_connect = session_search_index._connect_readonly
    original_candidate_scores = session_search_index._candidate_scores

    class FakeConn:
        def close(self):
            return None

    def fake_connect():
        return FakeConn()

    def slow_candidate_scores(_conn, _query, _limit, **_kwargs):
        time.sleep(0.2)
        return [("sid-bounded", 4)]

    session_search_index._connect_readonly = fake_connect
    session_search_index._candidate_scores = slow_candidate_scores
    started = time.monotonic()
    try:
        first = session_search_index.search(
            "bounded-query",
            limit=5,
            max_wait_seconds=0.01,
        )
        elapsed = time.monotonic() - started
        deadline = time.monotonic() + 1.0
        second = []
        while time.monotonic() < deadline:
            second = session_search_index.search(
                "bounded-query",
                limit=5,
                max_wait_seconds=0.01,
            )
            if second:
                break
            time.sleep(0.02)
    finally:
        session_search_index._connect_readonly = original_connect
        session_search_index._candidate_scores = original_candidate_scores
        session_search_index._search_inflight.clear()
    ok = elapsed < 0.08 and first == [] and second == [{"session_id": "sid-bounded", "score": 4}]
    print(
        f"{PASS if ok else FAIL} bounded search returns while cache fills "
        f"-- elapsed={elapsed:.3f}s first={first} second={second}",
    )
    return ok


def identical_searches_coalesce_test() -> bool:
    session_search_index._search_cache.clear()
    session_search_index._search_inflight.clear()
    original_connect = session_search_index._connect_readonly
    original_candidate_scores = session_search_index._candidate_scores
    calls = 0
    calls_lock = threading.Lock()

    class FakeConn:
        def close(self):
            return None

    def fake_connect():
        return FakeConn()

    def slow_candidate_scores(_conn, _query, _limit, **_kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.1)
        return [("sid-coalesced", 3)]

    session_search_index._connect_readonly = fake_connect
    session_search_index._candidate_scores = slow_candidate_scores
    try:
        results = []
        threads = [
            threading.Thread(
                target=lambda: results.append(session_search_index.search("same-query", limit=5))
            )
            for _ in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        session_search_index._connect_readonly = original_connect
        session_search_index._candidate_scores = original_candidate_scores
        session_search_index._search_inflight.clear()
    ok = calls == 1 and len(results) == 6 and all(
        result == [{"session_id": "sid-coalesced", "score": 3}]
        for result in results
    )
    print(f"{PASS if ok else FAIL} identical cold searches coalesce -- calls={calls}")
    return ok


def smaller_search_coalesces_with_larger_inflight_test() -> bool:
    session_search_index._search_cache.clear()
    session_search_index._search_inflight.clear()
    original_connect = session_search_index._connect_readonly
    original_candidate_scores = session_search_index._candidate_scores
    calls: list[int] = []
    calls_lock = threading.Lock()

    class FakeConn:
        def close(self):
            return None

    def fake_connect():
        return FakeConn()

    def slow_candidate_scores(_conn, _query, limit, **_kwargs):
        with calls_lock:
            calls.append(limit)
        time.sleep(0.1)
        return [(f"sid-{i}", limit - i) for i in range(limit)]

    session_search_index._connect_readonly = fake_connect
    session_search_index._candidate_scores = slow_candidate_scores
    results: dict[str, list[dict]] = {}
    try:
        larger = threading.Thread(
            target=lambda: results.setdefault(
                "larger",
                session_search_index.search("shared-limit-query", limit=20),
            )
        )
        smaller = threading.Thread(
            target=lambda: results.setdefault(
                "smaller",
                session_search_index.search("shared-limit-query", limit=5),
            )
        )
        larger.start()
        time.sleep(0.02)
        smaller.start()
        larger.join()
        smaller.join()
    finally:
        session_search_index._connect_readonly = original_connect
        session_search_index._candidate_scores = original_candidate_scores
        session_search_index._search_cache.clear()
        session_search_index._search_inflight.clear()
    ok = (
        calls == [20]
        and len(results.get("larger") or []) == 20
        and results.get("smaller") == results["larger"][:5]
    )
    print(
        f"{PASS if ok else FAIL} smaller cold search reuses larger in-flight search "
        f"-- calls={calls}",
    )
    return ok


def content_search_caps_matched_rows_test() -> bool:
    class FakeConn:
        def __init__(self):
            self.params = None

        def execute(self, _sql, params):
            self.params = params
            return self

        def fetchall(self):
            return []

    conn = FakeConn()
    session_search_index._candidate_scores(conn, "common-term", 7)
    ok = conn.params == (
        session_search_index._match_literal("common-term"),
        session_search_index._MATCHED_ROW_SCAN_LIMIT,
        7,
    )
    print(
        f"{PASS if ok else FAIL} content search caps matched-row scan "
        f"-- params={conn.params}",
    )
    return ok


def larger_cached_search_satisfies_smaller_limit_test() -> bool:
    session_search_index._search_cache.clear()
    session_search_index._search_inflight.clear()
    original_connect = session_search_index._connect_readonly
    original_candidate_scores = session_search_index._candidate_scores
    calls = 0

    class FakeConn:
        def close(self):
            return None

    def fake_connect():
        return FakeConn()

    def candidate_scores(_conn, _query, limit, **_kwargs):
        nonlocal calls
        calls += 1
        return [(f"sid-{i}", limit - i) for i in range(limit)]

    session_search_index._connect_readonly = fake_connect
    session_search_index._candidate_scores = candidate_scores
    try:
        first = session_search_index.search("reuse-query", limit=20)
        second = session_search_index.search("reuse-query", limit=5)
    finally:
        session_search_index._connect_readonly = original_connect
        session_search_index._candidate_scores = original_candidate_scores
        session_search_index._search_cache.clear()
        session_search_index._search_inflight.clear()
    ok = (
        calls == 1
        and len(first) == 20
        and second == first[:5]
    )
    print(
        f"{PASS if ok else FAIL} larger cached search satisfies smaller limit "
        f"-- calls={calls}",
    )
    return ok


if __name__ == "__main__":
    try:
        ok = index_event_nonblocking_test()
        ok = search_does_not_wait_for_pending_index_test() and ok
        ok = grep_sessions_passes_bounded_limit_test() and ok
        ok = bounded_search_returns_while_cache_fills_test() and ok
        ok = identical_searches_coalesce_test() and ok
        ok = smaller_search_coalesces_with_larger_inflight_test() and ok
        ok = content_search_caps_matched_rows_test() and ok
        ok = larger_cached_search_satisfies_smaller_limit_test() and ok
        ok = main_test() and ok
        sys.exit(0 if ok else 1)
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
