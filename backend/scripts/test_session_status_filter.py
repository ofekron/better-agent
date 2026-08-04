"""Status include/exclude filter regression tests for the sidebar list.

Locks the backend half of "select which statuses to show or filter out":
  1. `session_status.project` names and ranks the same buckets
     orders (rank stays the reverse index, so the sort is unchanged).
  2. `_session_status_gate` semantics: include-empty means every bucket,
     exclude always wins, and no selection at all yields no gate.
  3. The gate reaches every filter path — `_session_matches_list_filters`
     and each wrapper that pages/sorts through it.
  4. Every store-order fast path refuses to page when a status filter is
     active (those paths skip the filter funnel, so taking them would
     silently ignore the filter).

Run with:
    cd backend && .venv/bin/python scripts/test_session_status_filter.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home  # noqa: E402
_test_home.isolate("bc_test_status_filter_")

import main  # noqa: E402
import session_list_cache  # noqa: E402
import session_listing_api  # noqa: E402
import session_status  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    if not cond:
        failures.append(name)
        print(f"FAIL {name}")
    else:
        print(f"ok   {name}")


MON = {"run": "active", "blocked": "blocked_on_user"}
UNREAD = {"new": 3}
SNAPSHOT = (set(), MON, UNREAD, {})

NO_FILTERS = dict(
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
)

SESSIONS = [
    {"id": "err", "has_error": True, "updated_at": "2026-01-05T00:00:00Z"},
    {"id": "blocked", "updated_at": "2026-01-04T00:00:00Z"},
    {"id": "new", "updated_at": "2026-01-03T00:00:00Z"},
    {"id": "run", "updated_at": "2026-01-02T00:00:00Z"},
    {"id": "quiet", "updated_at": "2026-01-01T00:00:00Z"},
]


def ids(rows: list[dict]) -> list[str]:
    return [row["id"] for row in rows]


# ── 1. keys mirror ranks ─────────────────────────────────────────────────
def key(session: dict) -> str:
    return session_status.project(session, MON, UNREAD, {})["status_key"]


check("key.error", key({"id": "err", "has_error": True}) == "error")
check("key.needs_decision", key({"id": "blocked"}) == "needs_decision")
check("key.unread", key({"id": "new"}) == "unread")
check("key.open_work", key({"id": "x", "current_todos": [{"status": "pending"}]}) == "open_work")
check("key.running", key({"id": "run"}) == "running")
check("key.all_done", key({"id": "x", "markers": {"e": {"tag": session_status.MARKER_TAG_ALL_TASKS_DONE}}}) == "all_done")
check("key.idle", key({"id": "quiet"}) == "idle")
check(
    "key.rank_parity",
    all(
        session_status.project(session, MON, UNREAD, {})["status_rank"]
        == len(session_status.SESSION_STATUS_KEYS) - 1 - session_status.SESSION_STATUS_KEYS.index(key(session))
        for session in SESSIONS
    ),
)

# ── 2. gate semantics ────────────────────────────────────────────────────
check("gate.none_when_empty", session_listing_api._session_status_gate(frozenset(), frozenset(), SNAPSHOT) is None)

include_only = session_listing_api._session_status_gate(frozenset({"error", "running"}), frozenset(), SNAPSHOT)
check("gate.include.keeps", include_only({"id": "err", "has_error": True}) is True)
check("gate.include.keeps_second", include_only({"id": "run"}) is True)
check("gate.include.drops_others", include_only({"id": "quiet"}) is False)

exclude_only = session_listing_api._session_status_gate(frozenset(), frozenset({"idle"}), SNAPSHOT)
check("gate.exclude.drops", exclude_only({"id": "quiet"}) is False)
check("gate.exclude.keeps_rest", exclude_only({"id": "run"}) is True)

both = session_listing_api._session_status_gate(frozenset({"idle", "running"}), frozenset({"idle"}), SNAPSHOT)
check("gate.exclude_wins", both({"id": "quiet"}) is False)
check("gate.include_still_applies", both({"id": "run"}) is True)
check("gate.include_excludes_unlisted", both({"id": "new"}) is False)

check("split.drops_unknown", session_listing_api._split_session_statuses("error,bogus,idle") == frozenset({"error", "idle"}))
check("split.empty", session_listing_api._split_session_statuses(None) == frozenset())

# ── 3. the gate reaches every filter path ────────────────────────────────
gate = session_listing_api._session_status_gate(frozenset(), frozenset({"idle", "unread"}), SNAPSHOT)

check(
    "funnel.matches_list_filters",
    session_listing_api._session_matches_list_filters({"id": "quiet"}, **NO_FILTERS, status_gate=gate) is False
    and session_listing_api._session_matches_list_filters({"id": "run"}, **NO_FILTERS, status_gate=gate) is True,
)

sorted_rows = session_listing_api._filter_sort_sessions_for_list(
    list(SESSIONS),
    project_path=None,
    search=None,
    show_archived=False,
    file_edit_mode=None,
    folder_ids=set(),
    folder_view=False,
    tag_ids=set(),
    provider_ids=set(),
    model_ids=set(),
    modes=set(),
    sources=set(),
    content_scores={},
    sort_by="updated_at",
    status_gate=gate,
)
check("funnel.filter_sort_sessions", ids(sorted_rows) == ["err", "blocked", "run"])

page, total = session_listing_api._filter_sort_page_for_list(
    list(SESSIONS),
    offset=0,
    limit=2,
    project_path=None,
    search=None,
    show_archived=False,
    file_edit_mode=None,
    folder_ids=set(),
    folder_view=False,
    tag_ids=set(),
    provider_ids=set(),
    model_ids=set(),
    modes=set(),
    sources=set(),
    content_scores={},
    sort_by="updated_at",
    status_gate=gate,
)
check("funnel.filter_sort_page.total_excludes_filtered", total == 3)
check("funnel.filter_sort_page.page", ids(page) == ["err", "blocked"])

preserved = session_listing_api._filter_sessions_for_list_preserving_order(
    list(SESSIONS), **NO_FILTERS, status_gate=gate
)
check("funnel.preserving_order", ids(preserved) == ["err", "blocked", "run"])

page2, total2 = session_listing_api._filter_page_for_list_preserving_order(
    list(SESSIONS), offset=1, limit=2, **NO_FILTERS, status_gate=gate
)
check("funnel.preserving_order_page", ids(page2) == ["blocked", "run"] and total2 == 3)

check(
    "funnel.no_gate_keeps_all",
    ids(session_listing_api._filter_sessions_for_list_preserving_order(list(SESSIONS), **NO_FILTERS)) == ids(SESSIONS),
)

# ── 4. fast paths stand down while a status filter is active ─────────────
check(
    "fastpath.local_visible_order",
    session_listing_api._can_page_default_local_visible_order(
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
        status_filter=True,
    )
    is False,
)
check(
    "fastpath.local_summary_order",
    session_listing_api._can_page_local_summary_order(
        search_query="", folder_view=False, sort_by="updated_at", status_sort=False, status_filter=True
    )
    is False,
)
check(
    "fastpath.updated_at_with_virtual",
    session_listing_api._can_page_default_updated_at_with_virtual(
        search_query="",
        project_path=None,
        show_archived=False,
        file_edit_mode=None,
        folder_ids=set(),
        folder_view=False,
        tag_ids=set(),
        provider_ids=set(),
        model_ids=set(),
        modes=set(),
        sources=set(),
        sort_by="updated_at",
        status_sort=False,
        status_filter=True,
    )
    is False,
)
check(
    "fastpath.preserve_summary_order",
    session_listing_api._can_preserve_summary_order(
        search_query="",
        appended_virtual_sessions=False,
        folder_view=False,
        sort_by="updated_at",
        status_sort=False,
        status_filter=True,
    )
    is False,
)
check(
    "fastpath.local_search_scores",
    session_listing_api._can_page_local_search_scores(
        project_path=None,
        show_archived=False,
        file_edit_mode=None,
        folder_ids=set(),
        folder_view=False,
        tag_ids=set(),
        provider_ids=set(),
        model_ids=set(),
        modes=set(),
        sources=set(),
        sort_by="updated_at",
        status_sort=False,
        status_filter=True,
        connected=(),
    )
    is False,
)
check(
    "fastpath.still_eligible_without_status_filter",
    session_listing_api._can_page_local_summary_order(
        search_query="", folder_view=False, sort_by="updated_at", status_sort=False, status_filter=False
    )
    is True,
)

# ── 5. the route wires the query params to the gate ──────────────────────
class _FakeRequest:
    headers: dict[str, str] = {}


def route_ids(**params) -> list[str]:
    session_listing_api._local_session_summaries_for_sidebar = lambda: [dict(s) for s in SESSIONS]
    session_listing_api._local_session_summaries_by_ids_for_sidebar = lambda ids: [
        dict(s) for s in SESSIONS if s["id"] in set(ids)
    ]
    session_listing_api._sidebar_state_snapshot = lambda: SNAPSHOT
    session_listing_api._decorate_local_sidebar_sessions = lambda rows, _snapshot=None: list(rows)
    session_list_cache._sessions_list_cache_get = lambda *a, **k: None
    session_list_cache._sessions_list_response_maybe_cache = lambda _key, payload, **k: payload
    session_list_cache._schedule_session_event_meta_warm = lambda _page: None
    route_args = dict(
        offset=0,
        limit=50,
        project_path=None,
        search=None,
        show_archived=False,
        file_edit_mode=None,
        folder_ids=None,
        folder_view=False,
        tag_ids=None,
        provider_ids=None,
        model_ids=None,
        modes=None,
        sources=None,
        search_fields=None,
        sort_by="updated_at",
        statuses=None,
        exclude_statuses=None,
    )
    route_args.update(params)
    payload = asyncio.run(session_listing_api.get_sessions(request=_FakeRequest(), **route_args))
    return [row["id"] for row in payload["sessions"]]


check("route.include", set(route_ids(statuses="running,error")) == {"run", "err"})
check(
    "route.exclude",
    set(route_ids(exclude_statuses="idle,unread")) == {"err", "blocked", "run"},
)
# An unknown bucket builds no gate, so the route behaves exactly as it does
# with no status params at all (both stay on the store-order fast path).
check("route.unknown_bucket_ignored", route_ids(statuses="bogus") == route_ids())

if failures:
    print(f"\nFAILED {len(failures)}: {failures}")
    sys.exit(1)
print("\nPASS test_session_status_filter")
