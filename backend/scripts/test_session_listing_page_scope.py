"""`GET /api/sessions` is a PAGE, never a complete session snapshot.

The frontend `sessionRegistry` bootstraps and refreshes its per-session
`is_running` map from this endpoint. It used to apply the response as a
complete snapshot and evict every sid absent from it, so the OPEN session
silently degraded to `is_running: false` and the chat panel collapsed the
user's assistant response with no click.

This locks the backend half of that contract — the facts that make the
"page is not a snapshot" rule mandatory for any client:

  1. A no-query-params request (exactly what the registry bootstrap
     issues) is capped at the default page size, not the full corpus.
  2. A session ranked below that page is absent from it, while still
     being a live, listable session.
  3. An archived session is absent from the default page at ANY offset.

If any of these ever stop holding, the frontend's merge-not-replace rule
can be revisited; until then, evicting from this response is a bug.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate_installed("bc-test-session-listing-page-scope-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import auth  # noqa: E402
import remote_sessions_cache  # noqa: E402
import session_list_cache  # noqa: E402
import session_search_index  # noqa: E402
import session_store  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
HEADERS = {"Authorization": f"Bearer {auth.create_token('test')}"}

# Mirrors `Query(50, ...)` on `get_sessions` in session_listing_api.py.
DEFAULT_PAGE_LIMIT = 50
CWD = "/tmp/test-listing-page-scope"


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
    session_store._reset_home_scoped_caches()
    session_store._metadata_search_cache.clear()
    session_store._metadata_text_cache = ()
    session_store._metadata_text_cache_version = -1
    session_store._metadata_text_by_id_cache = {}
    session_store._metadata_text_by_id_cache_version = -1
    session_list_cache._sessions_list_response_cache.clear()
    session_list_cache._session_summaries_response_cache.clear()
    remote_sessions_cache.cache.clear()
    with session_search_index._lock:
        session_search_index._close_writer_connection_locked()
    session_search_index._close_readonly_connection()
    session_search_index._search_cache.clear()
    session_search_index._search_inflight.clear()
    session_search_index._index_generation += 1
    session_search_index._published_generation = session_search_index._index_generation
    session_search_index._published_generation_at = time.monotonic()
    (Path(_TMP_HOME) / "session_search_index.sqlite3").unlink(missing_ok=True)


def _record(sid: str, updated_at: str, **overrides: object) -> dict:
    record = {
        "_schema_version": session_store.SCHEMA_VERSION,
        "id": sid,
        "name": sid,
        "model": "gpt-5.5",
        "cwd": CWD,
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
        "pinned": False,
    }
    record.update(overrides)
    return record


def _write(record: dict) -> None:
    sessions_dir = Path(_TMP_HOME) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    with open(sessions_dir / f"{record['id']}.json", "w") as f:
        json.dump(record, f)
    session_store._upsert_summary(record)


def _seed_more_than_one_page() -> str:
    """Write one page's worth of newer sessions plus one older straggler.

    Returns the straggler's id — the session a client that trusts the
    default page would lose.
    """
    _reset_home()
    # Oldest → sorts last under the default (updated_at desc) ordering.
    _write(_record("below-the-page", "2026-06-01T00:00:00+00:00"))
    for index in range(DEFAULT_PAGE_LIMIT):
        _write(_record(
            f"on-the-page-{index:03d}",
            f"2026-07-{(index % 28) + 1:02d}T00:00:00+00:00",
        ))
    return "below-the-page"


def test_no_params_request_is_capped_at_one_page() -> None:
    """The registry bootstrap fetches `/api/sessions` with no params."""
    _seed_more_than_one_page()
    client = TestClient(main.app, client=("127.0.0.1", 50000))

    response = client.get("/api/sessions", headers=HEADERS)
    assert response.status_code == 200, f"/api/sessions status {response.status_code}"
    body = response.json()
    rows = body.get("sessions", [])
    assert len(rows) == DEFAULT_PAGE_LIMIT, (
        f"/api/sessions with no params returned {len(rows)} rows; "
        f"a no-param bootstrap request must be capped at one page of "
        f"{DEFAULT_PAGE_LIMIT}, not the whole corpus"
    )
    assert body.get("total") == DEFAULT_PAGE_LIMIT + 1, (
        f"/api/sessions total {body.get('total')}; expected "
        f"{DEFAULT_PAGE_LIMIT + 1} (the page plus the below-page straggler)"
    )
    print(
        f"{PASS} /api/sessions with no params returns one page "
        f"({len(rows)} of {body.get('total')}), not the whole corpus"
    )


def test_session_below_the_page_is_absent_but_alive() -> None:
    """Absence from the page is NOT evidence the session is gone."""
    straggler = _seed_more_than_one_page()
    client = TestClient(main.app, client=("127.0.0.1", 50000))

    default_page = client.get("/api/sessions", headers=HEADERS)
    deep_page = client.get(
        f"/api/sessions?offset={DEFAULT_PAGE_LIMIT}&limit={DEFAULT_PAGE_LIMIT}",
        headers=HEADERS,
    )
    detail = client.get(f"/api/sessions/{straggler}", headers=HEADERS)
    assert default_page.status_code == 200 and deep_page.status_code == 200, (
        f"listing status {default_page.status_code}/{deep_page.status_code}"
    )

    on_default = {row["id"] for row in default_page.json().get("sessions", [])}
    on_deep = {row["id"] for row in deep_page.json().get("sessions", [])}
    assert straggler not in on_default, (
        f"{straggler} ranked below the first page must be absent from the "
        f"default page; evicting it from a page is a snapshot bug"
    )
    assert straggler in on_deep, (
        f"{straggler} must appear on the deep page "
        f"(offset={DEFAULT_PAGE_LIMIT}); absence from one page is not death"
    )
    assert detail.status_code == 200, (
        f"detail fetch for {straggler} returned {detail.status_code}; "
        f"a session below the page must still be live and fetchable"
    )
    print(
        f"{PASS} a session below the first page is absent from the "
        f"default page while still live and fetchable"
    )


def test_archived_session_is_absent_from_every_default_page() -> None:
    """Archived sessions are filtered out regardless of offset."""
    _reset_home()
    _write(_record("plain", "2026-07-02T00:00:00+00:00"))
    _write(_record("archived-one", "2026-07-01T00:00:00+00:00", archived=True))
    client = TestClient(main.app, client=("127.0.0.1", 50000))

    response = client.get("/api/sessions?offset=0&limit=200", headers=HEADERS)
    assert response.status_code == 200, f"/api/sessions status {response.status_code}"
    ids = {row["id"] for row in response.json().get("sessions", [])}
    assert "plain" in ids, f"non-archived 'plain' session missing from listing; got {ids}"
    assert "archived-one" not in ids, (
        f"an archived session must never appear on a default listing page at "
        f"any offset; got {ids}"
    )
    print(
        f"{PASS} an archived session never appears on a default "
        f"listing page, at any offset"
    )


def main_run() -> int:
    try:
        test_no_params_request_is_capped_at_one_page()
        test_session_below_the_page_is_absent_but_alive()
        test_archived_session_is_absent_from_every_default_page()
        return 0
    except AssertionError as exc:
        print(f"{FAIL} {exc}")
        return 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main_run())
