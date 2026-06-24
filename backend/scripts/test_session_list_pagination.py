from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
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
        shutil.rmtree(sessions_dir)
    session_store._fork_index.clear()
    session_store._index_loaded = False
    session_store._summary_index.clear()
    session_store._summary_index_loaded = False
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
            f.write(json.dumps({"data": {"text": text}}) + "\n")
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

    response = client.get(
        "/api/sessions?search=needle&search_fields=content,title&offset=1&limit=1",
        headers=HEADERS,
    )
    if response.status_code != 200:
        print(f"{FAIL} /api/sessions search pagination status {response.status_code}")
        return False
    body = response.json()
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

    _write(_record("second", "2026-06-21T00:00:00+00:00"))
    _write_events("second", "needle")
    stale_during_write_burst = session_search_index.search("needle", limit=10)
    original_stale_seconds = session_search_index._SEARCH_CACHE_STALE_SECONDS
    session_search_index._SEARCH_CACHE_STALE_SECONDS = 0
    try:
        refreshed = session_search_index.search("needle", limit=10)
    finally:
        session_search_index._SEARCH_CACHE_STALE_SECONDS = original_stale_seconds
    ids = {row["session_id"] for row in refreshed}
    ok = (
        [row["session_id"] for row in first] == ["first"]
        and [row["session_id"] for row in cached] == ["first"]
        and [row["session_id"] for row in stale_during_write_burst] == ["first"]
        and ids == {"first", "second"}
    )
    print(f"{PASS if ok else FAIL} session search cache invalidates on write")
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

    response = client.post(
        "/api/sessions/keep/unpin-others",
        headers=HEADERS,
        json={"project_path": "/tmp/project-a"},
    )
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


def main_run() -> int:
    client = TestClient(main.app, client=("127.0.0.1", 50000))
    try:
        ok = True
        ok = test_paginates_after_global_sort(client) and ok
        ok = test_selected_session_does_not_override_pagination(client) and ok
        ok = test_filters_before_pagination(client) and ok
        ok = test_file_edit_mode_filters_before_pagination(client) and ok
        ok = test_search_content_filters_before_pagination(client) and ok
        ok = test_search_avoids_full_sidebar_list(client) and ok
        ok = test_search_index_cache_invalidates_on_write() and ok
        ok = test_unpin_others_ignores_backend_filters(client) and ok
        ok = test_pin_endpoint_unpins_specific_session(client) and ok
        ok = test_sidebar_strips_heavy_working_mode_meta(client) and ok
        ok = test_session_list_does_not_schedule_snapshot_prewarm(client) and ok
        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main_run())
