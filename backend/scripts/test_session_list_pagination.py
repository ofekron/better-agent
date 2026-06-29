from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-session-list-pagination-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import auth  # noqa: E402
import session_search_index  # noqa: E402
import session_store  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
HEADERS = {"Authorization": f"Bearer {auth.create_token('test')}"}


def _reset_home() -> None:
    sessions_dir = Path(_TMP_HOME) / "sessions"
    if sessions_dir.exists():
        for attempt in range(5):
            try:
                shutil.rmtree(sessions_dir)
                break
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(0.05)
    session_store._fork_index.clear()
    session_store._index_loaded = False
    session_store._summary_index.clear()
    session_store._summary_index_loaded = False
    session_store._metadata_search_cache.clear()
    session_store._metadata_text_cache = ()
    session_store._metadata_text_cache_version = -1
    session_store._metadata_trigram_index = {}
    session_store._metadata_trigram_index_version = -1
    main._sessions_list_response_cache.clear()
    main._session_summaries_response_cache.clear()
    main._remote_sessions_cache.clear()
    main._remote_sessions_cache_version = 0
    with session_search_index._lock:
        session_search_index._close_writer_connection_locked()
    session_search_index._close_readonly_connection()
    session_search_index._search_cache.clear()
    session_search_index._search_inflight.clear()
    session_search_index._index_generation += 1
    session_search_index._published_generation = session_search_index._index_generation
    session_search_index._published_generation_at = time.monotonic()
    index_path = Path(_TMP_HOME) / "session_search_index.sqlite3"
    index_path.unlink(missing_ok=True)


def _record(sid: str, updated_at: str, pinned: bool = False) -> dict:
    return {
        "_schema_version": session_store.SCHEMA_VERSION,
        "id": sid,
        "name": sid,
        "model": "gpt-5.5",
        "cwd": "/tmp/test-session-pagination",
        "orchestration_mode": "native",
        "kind": "user",
        "parent_session_id": None,
        "forks": [],
        "messages": [],
        "next_seq": 0,
        "created_at": "2026-06-16T00:00:00+00:00",
        "updated_at": updated_at,
        "source": "cli",
        "user_initiated": True,
        "pinned": pinned,
    }


def _record_with(
    sid: str,
    updated_at: str,
    **overrides: object,
) -> dict:
    record = _record(sid, updated_at)
    record.update(overrides)
    return record


def _write(record: dict) -> None:
    sessions_dir = Path(_TMP_HOME) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    with open(sessions_dir / f"{record['id']}.json", "w") as f:
        json.dump(record, f)
    session_store._upsert_summary(record)


def _write_events(sid: str, *texts: str) -> None:
    session_dir = Path(_TMP_HOME) / "sessions" / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    with open(session_dir / "events.jsonl", "w") as f:
        for text in texts:
            f.write(json.dumps({
                "type": "agent_message",
                "data": {
                    "message": {
                        "role": "user",
                        "content": text,
                    },
                },
            }) + "\n")
    session_search_index.rebuild_from_disk()


def test_paginates_after_global_sort(client: TestClient) -> bool:
    _reset_home()
    _write(_record("old", "2026-06-16T00:00:00+00:00"))
    _write(_record("new", "2026-06-18T00:00:00+00:00"))
    _write(_record("pinned-old", "2026-06-15T00:00:00+00:00", pinned=True))

    response = client.get("/api/sessions?offset=1&limit=1", headers=HEADERS)
    if response.status_code != 200:
        print(f"{FAIL} /api/sessions pagination status {response.status_code}")
        return False
    body = response.json()
    ids = [session["id"] for session in body.get("sessions", [])]
    ok = (
        ids == ["new"]
        and body.get("offset") == 1
        and body.get("limit") == 1
        and body.get("total") == 3
        and body.get("has_more") is True
    )
    print(f"{PASS if ok else FAIL} /api/sessions paginates after pinned/updated sort")
    return ok


def test_default_list_preserves_summary_order_without_resort(client: TestClient) -> bool:
    _reset_home()
    _write(_record("old", "2026-06-16T00:00:00+00:00"))
    _write(_record("new", "2026-06-18T00:00:00+00:00"))
    _write(_record("pinned-old", "2026-06-15T00:00:00+00:00", pinned=True))

    original = main._filter_sort_sessions_for_list
    original_prefs = main._session_list_user_prefs

    def fail_full_sort(*_args, **_kwargs):
        raise AssertionError("default session list should preserve summary order")

    main._filter_sort_sessions_for_list = fail_full_sort
    main._session_list_user_prefs = lambda: (False, "updated_at", False)
    try:
        response = client.get("/api/sessions?offset=1&limit=1", headers=HEADERS)
    finally:
        main._filter_sort_sessions_for_list = original
        main._session_list_user_prefs = original_prefs
    if response.status_code != 200:
        print(f"{FAIL} /api/sessions presorted fast path status {response.status_code}")
        return False
    body = response.json()
    ids = [session["id"] for session in body.get("sessions", [])]
    ok = ids == ["new"] and body.get("total") == 3
    print(f"{PASS if ok else FAIL} /api/sessions default list preserves summary order")
    return ok


def test_selected_session_does_not_override_pagination(client: TestClient) -> bool:
    _reset_home()
    _write(_record("old", "2026-06-16T00:00:00+00:00"))
    _write(_record("new", "2026-06-18T00:00:00+00:00"))
    _write(_record("pinned-old", "2026-06-15T00:00:00+00:00", pinned=True))

    response = client.get(
        "/api/sessions?selected_session_id=old&offset=1&limit=1",
        headers=HEADERS,
    )
    if response.status_code != 200:
        print(f"{FAIL} /api/sessions selected pagination status {response.status_code}")
        return False
    body = response.json()
    ids = [session["id"] for session in body.get("sessions", [])]
    ok = (
        ids == ["new"]
        and body.get("offset") == 1
        and body.get("limit") == 1
        and body.get("total") == 3
        and body.get("has_more") is True
    )
    print(f"{PASS if ok else FAIL} /api/sessions ignores selected id for pagination")
    return ok


def test_filters_before_pagination(client: TestClient) -> bool:
    _reset_home()
    _write(_record_with(
        "project-a-old",
        "2026-06-16T00:00:00+00:00",
        cwd="/tmp/project-a",
        provider_id="codex",
        model="gpt-5",
        orchestration_mode="native",
    ))
    _write(_record_with(
        "project-b-new",
        "2026-06-19T00:00:00+00:00",
        cwd="/tmp/project-b",
        provider_id="claude",
        model="sonnet",
        orchestration_mode="team",
    ))
    _write(_record_with(
        "project-a-new",
        "2026-06-18T00:00:00+00:00",
        cwd="/tmp/project-a",
        provider_id="codex",
        model="gpt-5",
        orchestration_mode="native",
    ))

    response = client.get(
        "/api/sessions?project_path=/tmp/project-a&provider_ids=codex"
        "&model_ids=gpt-5&modes=native&offset=1&limit=1",
        headers=HEADERS,
    )
    if response.status_code != 200:
        print(f"{FAIL} /api/sessions filtered pagination status {response.status_code}")
        return False
    body = response.json()
    ids = [session["id"] for session in body.get("sessions", [])]
    ok = (
        ids == ["project-a-old"]
        and body.get("total") == 2
        and body.get("has_more") is False
    )
    print(f"{PASS if ok else FAIL} /api/sessions filters before pagination")
    return ok


def test_file_edit_mode_filters_before_pagination(client: TestClient) -> bool:
    _reset_home()
    _write(_record_with(
        "normal-new",
        "2026-06-20T00:00:00+00:00",
    ))
    _write(_record_with(
        "file-edit-new",
        "2026-06-19T00:00:00+00:00",
        working_mode="file_editing",
        working_mode_meta={"persistent": True},
    ))
    _write(_record_with(
        "file-edit-old",
        "2026-06-18T00:00:00+00:00",
        working_mode="file_editing",
        working_mode_meta={"persistent": True},
    ))

    yes_response = client.get(
        "/api/sessions?file_edit_mode=true&offset=1&limit=1",
        headers=HEADERS,
    )
    no_response = client.get(
        "/api/sessions?file_edit_mode=false&offset=0&limit=10",
        headers=HEADERS,
    )
    if yes_response.status_code != 200 or no_response.status_code != 200:
        print(
            f"{FAIL} /api/sessions file edit mode filter status "
            f"{yes_response.status_code}/{no_response.status_code}"
        )
        return False
    yes_body = yes_response.json()
    no_body = no_response.json()
    yes_ids = [session["id"] for session in yes_body.get("sessions", [])]
    no_ids = [session["id"] for session in no_body.get("sessions", [])]
    ok = (
        yes_ids == ["file-edit-old"]
        and yes_body.get("total") == 2
        and yes_body.get("has_more") is False
        and no_ids == ["normal-new"]
        and no_body.get("total") == 1
    )
    print(f"{PASS if ok else FAIL} /api/sessions filters by file edit mode")
    return ok


def test_search_content_filters_before_pagination(client: TestClient) -> bool:
    _reset_home()
    _write(_record_with(
        "metadata-new",
        "2026-06-20T00:00:00+00:00",
        name="needle metadata",
    ))
    _write(_record("content-new", "2026-06-19T00:00:00+00:00"))
    _write_events("content-new", "needle")
    _write(_record("content-old", "2026-06-18T00:00:00+00:00"))
    _write_events("content-old", "needle", "needle")
    _write(_record("miss", "2026-06-21T00:00:00+00:00"))

    response = None
    body = {}
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        response = client.get(
            "/api/sessions?search=needle&search_fields=content,title&offset=1&limit=1",
            headers=HEADERS,
        )
        if response.status_code != 200:
            break
        body = response.json()
        if body.get("total") == 3:
            break
        time.sleep(0.05)
    if response is None:
        raise AssertionError("list response was not attempted")
    if response.status_code != 200:
        print(f"{FAIL} /api/sessions search pagination status {response.status_code}")
        return False
    ids = [session["id"] for session in body.get("sessions", [])]
    ok = (
        ids == ["metadata-new"]
        and body.get("offset") == 1
        and body.get("limit") == 1
        and body.get("total") == 3
        and body.get("has_more") is True
    )
    print(f"{PASS if ok else FAIL} /api/sessions search filters before pagination")
    return ok


def test_search_avoids_full_sidebar_list(client: TestClient) -> bool:
    _reset_home()
    _write(_record_with(
        "matched",
        "2026-06-20T00:00:00+00:00",
        name="needle title",
    ))
    _write(_record("miss", "2026-06-21T00:00:00+00:00"))

    original = main._local_session_summaries_for_sidebar

    def fail_full_list():
        raise AssertionError("search should not build the full sidebar list")

    main._local_session_summaries_for_sidebar = fail_full_list
    try:
        response = client.get("/api/sessions?search=needle", headers=HEADERS)
    finally:
        main._local_session_summaries_for_sidebar = original
    if response.status_code != 200:
        print(f"{FAIL} /api/sessions search fast path status {response.status_code}")
        return False
    ids = [session["id"] for session in response.json().get("sessions", [])]
    ok = ids == ["matched"]
    print(f"{PASS if ok else FAIL} /api/sessions search avoids full sidebar list")
    return ok


def test_simple_search_skips_generic_filter_sort(client: TestClient) -> bool:
    _reset_home()
    _write(_record_with(
        "older-hit",
        "2026-06-18T00:00:00+00:00",
        name="needle title needle",
    ))
    _write(_record_with(
        "newer-hit",
        "2026-06-20T00:00:00+00:00",
        name="needle title",
    ))
    _write(_record("miss", "2026-06-21T00:00:00+00:00"))

    original = main._filter_sort_page_for_list
    original_prefs = main._session_list_user_prefs

    def fail_filter_sort(*_args, **_kwargs):
        raise AssertionError("simple search should page ranked score results directly")

    main._filter_sort_page_for_list = fail_filter_sort
    main._session_list_user_prefs = lambda: (False, "updated_at", False)
    try:
        response = client.get(
            "/api/sessions?search=needle&search_fields=title&offset=1&limit=1",
            headers=HEADERS,
        )
    finally:
        main._filter_sort_page_for_list = original
        main._session_list_user_prefs = original_prefs
    if response.status_code != 200:
        print(f"{FAIL} /api/sessions simple search page status {response.status_code}")
        return False
    body = response.json()
    ids = [session["id"] for session in body.get("sessions", [])]
    ok = (
        ids == ["newer-hit"]
        and body.get("total") == 2
        and body.get("has_more") is False
        and body.get("sessions", [{}])[0].get("search_score") == 1
    )
    print(f"{PASS if ok else FAIL} /api/sessions simple search skips filter sort")
    return ok


def test_repeated_session_search_uses_response_cache(client: TestClient) -> bool:
    _reset_home()
    _write(_record_with(
        "matched",
        "2026-06-20T00:00:00+00:00",
        name="needle title",
    ))
    first = client.get(
        "/api/sessions?search=needle&search_fields=title",
        headers=HEADERS,
    )
    if first.status_code != 200:
        print(f"{FAIL} /api/sessions first cached search status {first.status_code}")
        return False

    original = main._build_local_sessions_page_for_list

    def fail_recompute(*_args, **_kwargs):
        raise AssertionError("identical session search should use response cache")

    main._build_local_sessions_page_for_list = fail_recompute
    try:
        second = client.get(
            "/api/sessions?search=needle&search_fields=title",
            headers=HEADERS,
        )
    except AssertionError:
        print(f"{FAIL} /api/sessions repeated search recomputed page")
        return False
    finally:
        main._build_local_sessions_page_for_list = original

    ids = [session["id"] for session in second.json().get("sessions", [])]
    ok = second.status_code == 200 and ids == ["matched"]
    print(f"{PASS if ok else FAIL} /api/sessions repeated search uses response cache")
    return ok


def test_repeated_session_summaries_uses_response_cache(client: TestClient) -> bool:
    _reset_home()
    _write(_record_with(
        "open-a",
        "2026-06-20T00:00:00+00:00",
        name="open a",
    ))
    session_store.wait_for_summary_index(1.0, min_published=1)

    first = client.get("/api/sessions/summaries?ids=open-a", headers=HEADERS)
    if first.status_code != 200:
        print(f"{FAIL} /api/sessions/summaries first status {first.status_code}")
        return False

    original = main._decorate_local_sidebar_sessions

    def fail_decorate(*_args, **_kwargs):
        raise AssertionError("identical summaries request should use response cache")

    main._decorate_local_sidebar_sessions = fail_decorate
    try:
        second = client.get("/api/sessions/summaries?ids=open-a", headers=HEADERS)
    finally:
        main._decorate_local_sidebar_sessions = original
    if second.status_code != 200:
        print(f"{FAIL} /api/sessions/summaries cached status {second.status_code}")
        return False
    ok = first.json() == second.json()
    print(f"{PASS if ok else FAIL} /api/sessions/summaries repeated request uses cache")
    return ok


def test_repeated_content_session_search_uses_response_cache(client: TestClient) -> bool:
    _reset_home()
    _write(_record("matched", "2026-06-20T00:00:00+00:00"))
    _write_events("matched", "needle body")
    first = client.get(
        "/api/sessions?search=needle&search_fields=content",
        headers=HEADERS,
    )
    if first.status_code != 200:
        print(f"{FAIL} /api/sessions first cached content search status {first.status_code}")
        return False

    deadline = time.monotonic() + 2.0
    content_ready = False
    while time.monotonic() < deadline:
        if session_search_index.has_cached_result(
            "needle",
            main._session_search_candidate_limit(0, 50),
        ):
            content_ready = True
            break
        time.sleep(0.05)
    if not content_ready:
        print(f"{FAIL} /api/sessions content search cache did not warm")
        return False
    warm = client.get(
        "/api/sessions?search=needle&search_fields=content",
        headers=HEADERS,
    )
    if warm.status_code != 200:
        print(f"{FAIL} /api/sessions warm content search status {warm.status_code}")
        return False

    original = main._build_local_sessions_page_for_list

    def fail_recompute(*_args, **_kwargs):
        raise AssertionError("identical content session search should use response cache")

    main._build_local_sessions_page_for_list = fail_recompute
    try:
        second = client.get(
            "/api/sessions?search=needle&search_fields=content",
            headers=HEADERS,
        )
    except AssertionError:
        print(f"{FAIL} /api/sessions repeated content search recomputed page")
        return False
    finally:
        main._build_local_sessions_page_for_list = original

    ids = [session["id"] for session in second.json().get("sessions", [])]
    ok = second.status_code == 200 and ids == ["matched"]
    print(f"{PASS if ok else FAIL} /api/sessions repeated content search uses response cache")
    return ok


def test_search_paginates_without_full_sort(client: TestClient) -> bool:
    _reset_home()
    for index in range(8):
        _write(_record_with(
            f"match-{index}",
            f"2026-06-2{index}T00:00:00+00:00",
            name="needle title",
        ))

    original = main._filter_sort_sessions_for_list

    def fail_full_sort(*_args, **_kwargs):
        raise AssertionError("search should select the requested page without full sort")

    main._filter_sort_sessions_for_list = fail_full_sort
    try:
        response = client.get(
            "/api/sessions?search=needle&search_fields=title&limit=2",
            headers=HEADERS,
        )
    except AssertionError:
        print(f"{FAIL} /api/sessions search used full sort")
        return False
    finally:
        main._filter_sort_sessions_for_list = original

    body = response.json()
    ok = (
        response.status_code == 200
        and body.get("total") == 8
        and len(body.get("sessions") or []) == 2
    )
    print(f"{PASS if ok else FAIL} /api/sessions search paginates without full sort")
    return ok


def test_search_index_cache_invalidates_on_write() -> bool:
    _reset_home()
    _write(_record("first", "2026-06-20T00:00:00+00:00"))
    _write_events("first", "needle")
    session_search_index._search_cache.clear()
    first = session_search_index.search("needle", limit=10)

    original = session_search_index._candidate_scores

    def fail_candidate_scores(*_args, **_kwargs):
        raise AssertionError("cached search should not hit sqlite")

    session_search_index._candidate_scores = fail_candidate_scores
    try:
        cached = session_search_index.search("needle", limit=10)
    finally:
        session_search_index._candidate_scores = original

    generation_before_write = session_search_index.generation()
    session_search_index._apply_rows([("second", "needle")])
    generation_during_write_burst = session_search_index.generation()
    stale_during_write_burst = session_search_index.search("needle", limit=10)
    original_stale_seconds = session_search_index._SEARCH_CACHE_STALE_SECONDS
    original_published_at = session_search_index._published_generation_at
    session_search_index._SEARCH_CACHE_STALE_SECONDS = 0
    session_search_index._published_generation_at = 0
    try:
        session_search_index._apply_rows([("third", "needle")])
        generation_after_stale_window = session_search_index.generation()
        refreshed = session_search_index.search("needle", limit=10)
    finally:
        session_search_index._SEARCH_CACHE_STALE_SECONDS = original_stale_seconds
        session_search_index._published_generation_at = original_published_at
    ids = {row["session_id"] for row in refreshed}
    ok = (
        [row["session_id"] for row in first] == ["first"]
        and [row["session_id"] for row in cached] == ["first"]
        and generation_during_write_burst == generation_before_write
        and generation_after_stale_window != generation_before_write
        and [row["session_id"] for row in stale_during_write_burst] == ["first"]
        and ids == {"first", "second", "third"}
    )
    print(f"{PASS if ok else FAIL} session search cache invalidates on write")
    return ok


def test_metadata_search_uses_trigram_candidates() -> bool:
    _reset_home()
    _write(_record_with(
        "match-title",
        "2026-06-20T00:00:00+00:00",
        name="alpha unique needle",
    ))
    _write(_record_with(
        "match-first-prompt",
        "2026-06-19T00:00:00+00:00",
        messages=[{
            "id": "u1",
            "role": "user",
            "content": "first prompt has unique needle",
            "timestamp": "2026-06-19T00:00:00+00:00",
        }],
    ))
    _write(_record_with(
        "miss",
        "2026-06-18T00:00:00+00:00",
        name="totally unrelated",
        messages=[{
            "id": "u1",
            "role": "user",
            "content": "nothing relevant",
            "timestamp": "2026-06-18T00:00:00+00:00",
        }],
    ))

    original_rows = session_store._metadata_search_rows
    row_calls = 0

    def counted_rows():
        nonlocal row_calls
        row_calls += 1
        return original_rows()

    session_store._metadata_search_rows = counted_rows
    try:
        first = session_store.grep_session_scores("unique needle")
        second = session_store.grep_session_scores("unique needle")
    finally:
        session_store._metadata_search_rows = original_rows

    ok = (
        set(first) == {"match-title", "match-first-prompt"}
        and second == first
        and row_calls == 2
        and session_store._metadata_trigram_index_version == session_store.search_metadata_version()
    )
    print(f"{PASS if ok else FAIL} metadata search uses trigram candidates")
    return ok


def test_metadata_trigram_search_preserves_substring_behavior() -> bool:
    _reset_home()
    _write(_record_with(
        "substring",
        "2026-06-20T00:00:00+00:00",
        name="prefixabcsuffix",
    ))
    _write(_record_with(
        "spaced",
        "2026-06-19T00:00:00+00:00",
        name="prefix abc suffix",
    ))

    scores = session_store.grep_session_scores("xabcs", {session_store.SEARCH_FIELD_TITLE})
    ok = scores == {"substring": 1}
    print(f"{PASS if ok else FAIL} metadata trigram search preserves substring behavior")
    return ok


def test_unpin_others_ignores_backend_filters(client: TestClient) -> bool:
    _reset_home()
    _write(_record_with(
        "keep",
        "2026-06-19T00:00:00+00:00",
        cwd="/tmp/project-a",
        pinned=True,
    ))
    _write(_record_with(
        "matching-paged-out",
        "2026-06-18T00:00:00+00:00",
        cwd="/tmp/project-a",
        pinned=True,
    ))
    _write(_record_with(
        "other-project",
        "2026-06-17T00:00:00+00:00",
        cwd="/tmp/project-b",
        pinned=True,
    ))

    original = main._local_sessions_for_sidebar
    main._local_sessions_for_sidebar = lambda: (_ for _ in ()).throw(
        AssertionError("unpin-others must not build the decorated sidebar list")
    )
    try:
        response = client.post(
            "/api/sessions/keep/unpin-others",
            headers=HEADERS,
            json={"project_path": "/tmp/project-a"},
        )
    finally:
        main._local_sessions_for_sidebar = original
    if response.status_code != 200:
        print(f"{FAIL} /api/sessions unpin-others status {response.status_code}")
        return False
    body = response.json()
    listing = client.get("/api/sessions?offset=0&limit=10", headers=HEADERS).json()
    pinned_by_id = {
        session["id"]: session.get("pinned", False)
        for session in listing.get("sessions", [])
    }
    updated_by_id = {
        session["id"]: session.get("updated_at")
        for session in listing.get("sessions", [])
    }
    ok = (
        body.get("unpinned_ids") == ["matching-paged-out", "other-project"]
        and pinned_by_id.get("keep") is True
        and pinned_by_id.get("matching-paged-out") is False
        and pinned_by_id.get("other-project") is False
        and updated_by_id.get("matching-paged-out") == "2026-06-18T00:00:00+00:00"
        and updated_by_id.get("other-project") == "2026-06-17T00:00:00+00:00"
    )
    print(f"{PASS if ok else FAIL} /api/sessions unpin-others ignores backend filters")
    return ok


def test_new_session_defaults_to_pinned_and_sorts_above_pinned(client: TestClient) -> bool:
    _reset_home()
    _write(_record("older-pinned", "2026-06-19T00:00:00+00:00", pinned=True))

    response = client.post(
        "/api/sessions",
        headers=HEADERS,
        json={"orchestration_mode": "native", "cwd": "/tmp/test-session-pagination"},
    )
    if response.status_code != 200:
        print(f"{FAIL} /api/sessions create status {response.status_code}")
        return False
    created = response.json()
    listing = client.get("/api/sessions?offset=0&limit=10", headers=HEADERS).json()
    ids = [session["id"] for session in listing.get("sessions", [])]
    by_id = {session["id"]: session for session in listing.get("sessions", [])}
    ok = (
        created.get("pinned") is True
        and by_id.get(created.get("id"), {}).get("pinned") is True
        and ids[:2] == [created.get("id"), "older-pinned"]
    )
    print(f"{PASS if ok else FAIL} new sessions default pinned and sort above pinned")
    return ok


def test_pin_endpoint_unpins_specific_session(client: TestClient) -> bool:
    _reset_home()
    _write(_record("specific", "2026-06-19T00:00:00+00:00", pinned=True))

    response = client.put(
        "/api/sessions/specific/pin",
        headers=HEADERS,
        json={"pinned": False},
    )
    if response.status_code != 200:
        print(f"{FAIL} /api/sessions pin false status {response.status_code}")
        return False
    listing = client.get("/api/sessions?offset=0&limit=10", headers=HEADERS).json()
    pinned_by_id = {
        session["id"]: session.get("pinned", False)
        for session in listing.get("sessions", [])
    }
    updated_by_id = {
        session["id"]: session.get("updated_at")
        for session in listing.get("sessions", [])
    }
    ok = (
        response.json().get("pinned") is False
        and pinned_by_id.get("specific") is False
        and updated_by_id.get("specific") == "2026-06-19T00:00:00+00:00"
    )
    print(f"{PASS if ok else FAIL} /api/sessions pin endpoint unpins one session")
    return ok


def test_topbar_pin_endpoint_lists_pinned_sessions(client: TestClient) -> bool:
    _reset_home()
    _write(_record_with(
        "topbar",
        "2026-06-19T00:00:00+00:00",
        topbar_pinned=False,
    ))
    _write(_record_with(
        "normal",
        "2026-06-18T00:00:00+00:00",
        topbar_pinned=False,
    ))

    response = client.put(
        "/api/sessions/topbar/topbar-pin",
        headers=HEADERS,
        json={"pinned": True},
    )
    if response.status_code != 200:
        print(f"{FAIL} /api/sessions topbar-pin status {response.status_code}")
        return False
    listing = client.get("/api/sessions/topbar-pinned", headers=HEADERS)
    if listing.status_code != 200:
        print(f"{FAIL} /api/sessions/topbar-pinned status {listing.status_code}")
        return False
    sessions = listing.json().get("sessions", [])
    by_id = {session["id"]: session for session in sessions}
    updated = client.get("/api/sessions?offset=0&limit=10", headers=HEADERS).json()
    updated_by_id = {
        session["id"]: session.get("updated_at")
        for session in updated.get("sessions", [])
    }
    ok = (
        response.json().get("topbar_pinned") is True
        and [session.get("id") for session in sessions] == ["topbar"]
        and by_id["topbar"].get("topbar_pinned") is True
        and isinstance(by_id["topbar"].get("topbar_pinned_at"), str)
        and updated_by_id.get("topbar") == "2026-06-19T00:00:00+00:00"
    )
    print(f"{PASS if ok else FAIL} /api/sessions topbar pin lists pinned sessions")
    return ok


def test_sidebar_strips_heavy_working_mode_meta(client: TestClient) -> bool:
    _reset_home()
    _write(_record_with(
        "file-edit",
        "2026-06-19T00:00:00+00:00",
        working_mode="file_editing",
        working_mode_meta={
            "persistent": True,
            "file_paths": ["/tmp/a.py"],
            "original_contents": {"/tmp/a.py": "x" * 10_000},
            "file_discussions": [{"id": "d", "messages": ["x" * 10_000]}],
        },
    ))

    response = client.get("/api/sessions?offset=0&limit=10", headers=HEADERS)
    if response.status_code != 200:
        print(f"{FAIL} /api/sessions sidebar meta trim status {response.status_code}")
        return False
    sessions = response.json().get("sessions") or []
    meta = next((s.get("working_mode_meta") for s in sessions if s.get("id") == "file-edit"), None)
    ok = (
        isinstance(meta, dict)
        and meta.get("persistent") is True
        and meta.get("file_paths") == ["/tmp/a.py"]
        and "original_contents" not in meta
        and "file_discussions" not in meta
    )
    print(f"{PASS if ok else FAIL} /api/sessions strips heavy working mode meta")
    return ok


def test_session_list_source_filter_user_awareness(client: TestClient) -> bool:
    _reset_home()
    _write(_record("human", "2026-06-18T00:00:00+00:00"))
    system = _record("system", "2026-06-19T00:00:00+00:00")
    system["source"] = "internal"
    system["user_initiated"] = False
    _write(system)

    user_resp = client.get("/api/sessions?sources=user", headers=HEADERS)
    system_resp = client.get("/api/sessions?sources=system", headers=HEADERS)
    internal_resp = client.get("/api/sessions?sources=internal", headers=HEADERS)
    if user_resp.status_code != 200 or system_resp.status_code != 200 or internal_resp.status_code != 200:
        print(
            f"{FAIL} /api/sessions source-awareness status "
            f"user={user_resp.status_code} system={system_resp.status_code} "
            f"internal={internal_resp.status_code}"
        )
        return False
    user_ids = {s.get("id") for s in user_resp.json().get("sessions") or []}
    system_ids = {s.get("id") for s in system_resp.json().get("sessions") or []}
    internal_ids = {s.get("id") for s in internal_resp.json().get("sessions") or []}
    ok = user_ids == {"human"} and system_ids == {"system"} and internal_ids == {"system"}
    print(
        f"{PASS if ok else FAIL} /api/sessions source filter distinguishes "
        f"user-aware vs system-aware"
        f"{'' if ok else f' — user={user_ids} system={system_ids} internal={internal_ids}'}"
    )
    return ok


def test_session_list_does_not_schedule_snapshot_prewarm(client: TestClient) -> bool:
    _reset_home()
    _write(_record("old", "2026-06-16T00:00:00+00:00"))
    _write(_record("new", "2026-06-18T00:00:00+00:00"))
    response = client.get("/api/sessions?offset=0&limit=1", headers=HEADERS)
    if response.status_code != 200:
        print(f"{FAIL} /api/sessions no-prewarm status {response.status_code}")
        return False
    ok = not hasattr(main, "_schedule_session_snapshot_prewarm")
    print(f"{PASS if ok else FAIL} /api/sessions does not schedule snapshot prewarm")
    return ok


def test_connected_first_page_caps_remote_cache_copy(client: TestClient) -> bool:
    _reset_home()
    for index in range(3):
        _write(_record(
            f"local-{index}",
            f"2026-06-2{index}T00:00:00+00:00",
        ))
    remote = [
        {
            "id": f"remote-{index}",
            "name": f"remote-{index}",
            "updated_at": "2026-06-19T00:00:00+00:00",
            "created_at": "2026-06-19T00:00:00+00:00",
        }
        for index in range(100)
    ]
    main._remote_sessions_cache["node-a"] = (time.monotonic(), remote)
    main._remote_sessions_cache_version += 1

    fake_node_store = types.SimpleNamespace(
        connected_worker_node_ids_snapshot=lambda: (1, ("node-a",)),
    )
    original_node_store = sys.modules.get("node_store")
    original_enabled = main._machine_nodes_enabled_cached
    original_prefs = main._session_list_user_prefs
    copied_lengths: list[int] = []
    original_copy = main._copy_remote_sessions

    def tracking_copy(sessions, *, limit=None):
        copied = original_copy(sessions, limit=limit)
        if limit is not None:
            copied_lengths.append(len(copied))
        return copied

    sys.modules["node_store"] = fake_node_store
    main._machine_nodes_enabled_cached = lambda: True
    main._session_list_user_prefs = lambda: (False, "updated_at", False)
    main._copy_remote_sessions = tracking_copy
    try:
        response = client.get("/api/sessions?offset=0&limit=2", headers=HEADERS)
    finally:
        main._copy_remote_sessions = original_copy
        main._session_list_user_prefs = original_prefs
        main._machine_nodes_enabled_cached = original_enabled
        if original_node_store is None:
            sys.modules.pop("node_store", None)
        else:
            sys.modules["node_store"] = original_node_store
    if response.status_code != 200:
        print(f"{FAIL} connected /api/sessions capped remote status {response.status_code}")
        return False
    body = response.json()
    ok = (
        body.get("total") == 103
        and len(body.get("sessions") or []) == 2
        and copied_lengths
        and max(copied_lengths) <= 2
    )
    print(f"{PASS if ok else FAIL} connected /api/sessions caps remote cache copy")
    return ok


def main_run() -> int:
    client = TestClient(main.app, client=("127.0.0.1", 50000))
    try:
        ok = True
        ok = test_paginates_after_global_sort(client) and ok
        ok = test_default_list_preserves_summary_order_without_resort(client) and ok
        ok = test_selected_session_does_not_override_pagination(client) and ok
        ok = test_filters_before_pagination(client) and ok
        ok = test_file_edit_mode_filters_before_pagination(client) and ok
        ok = test_search_content_filters_before_pagination(client) and ok
        ok = test_search_avoids_full_sidebar_list(client) and ok
        ok = test_simple_search_skips_generic_filter_sort(client) and ok
        ok = test_repeated_session_search_uses_response_cache(client) and ok
        ok = test_repeated_session_summaries_uses_response_cache(client) and ok
        ok = test_repeated_content_session_search_uses_response_cache(client) and ok
        ok = test_search_paginates_without_full_sort(client) and ok
        ok = test_search_index_cache_invalidates_on_write() and ok
        ok = test_metadata_search_uses_trigram_candidates() and ok
        ok = test_metadata_trigram_search_preserves_substring_behavior() and ok
        ok = test_unpin_others_ignores_backend_filters(client) and ok
        ok = test_new_session_defaults_to_pinned_and_sorts_above_pinned(client) and ok
        ok = test_pin_endpoint_unpins_specific_session(client) and ok
        ok = test_topbar_pin_endpoint_lists_pinned_sessions(client) and ok
        ok = test_sidebar_strips_heavy_working_mode_meta(client) and ok
        ok = test_session_list_source_filter_user_awareness(client) and ok
        ok = test_session_list_does_not_schedule_snapshot_prewarm(client) and ok
        ok = test_connected_first_page_caps_remote_cache_copy(client) and ok
        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main_run())
