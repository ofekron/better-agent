"""Status-sort rank + sort-key regression tests.

Locks the backend half of the "group by status" sort option:
  1. `_session_status_rank` buckets correctly (4 running, 3 needs-decision,
     2 has-new, 1 all-done, 0 none) with highest-wins precedence, reading
     monitoring snapshot first then row fallback, and marker TAG (not color).
  2. `_session_list_sort_key` puts status BELOW empty-new + pinned and ABOVE
     the timestamp (the decided precedence: empty > pinned > status > ts),
     and only when status_sort=True.
  3. `_session_filtered_sort_key` (search mode) keeps relevance dominant:
     status sits BELOW search_score, above ts.

Run with:
    cd backend && .venv/bin/python scripts/test_status_sort_rank.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home  # noqa: E402
_test_home.isolate("bc_test_status_sort_")

import main  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    if not cond:
        failures.append(name)
        print(f"FAIL {name}")
    else:
        print(f"ok   {name}")


NEEDS = main._MARKER_TAG_NEEDS_DECISION
DONE = main._MARKER_TAG_ALL_TASKS_DONE


def marker(tag: str) -> dict:
    return {"ext": {"color": "#x", "tooltip": "t", "tag": tag}}


# ── 1. rank buckets ──────────────────────────────────────────────────────
mon = {
    "run": "active",
    "bg": "waiting_on_background",
    "blocked": "blocked_on_user",
    "idle": "idle",
}
unread = {"new": 3}

check("rank.running.active", main._session_status_rank({"id": "run"}, mon, unread) == 4)
check("rank.running.waiting_bg", main._session_status_rank({"id": "bg"}, mon, unread) == 4)
check("rank.needs.blocked_state", main._session_status_rank({"id": "blocked"}, mon, unread) == 3)
check(
    "rank.needs.marker_tag",
    main._session_status_rank({"id": "idle", "markers": marker(NEEDS)}, mon, unread) == 3,
)
check("rank.hasnew.unread", main._session_status_rank({"id": "new"}, mon, unread) == 2)
check(
    "rank.alldone.marker_tag",
    main._session_status_rank({"id": "idle", "markers": marker(DONE)}, mon, unread) == 1,
)
check("rank.none", main._session_status_rank({"id": "idle"}, mon, unread) == 0)

# precedence: running beats a needs-decision marker on the same session
check(
    "rank.precedence.running_over_marker",
    main._session_status_rank({"id": "run", "markers": marker(NEEDS)}, mon, unread) == 4,
)
# precedence: needs-decision beats unread
check(
    "rank.precedence.needs_over_unread",
    main._session_status_rank({"id": "new", "markers": marker(NEEDS)}, mon, unread) == 3,
)
# classification is by TAG, not color/tooltip — a marker with no tag is inert
check(
    "rank.untagged_marker_inert",
    main._session_status_rank(
        {"id": "idle", "markers": {"ext": {"color": "#d29922", "tooltip": "x"}}},
        mon, unread,
    ) == 0,
)
# row fallback: sid absent from snapshot → read the row's own fields
check(
    "rank.row_fallback.monitoring",
    main._session_status_rank({"id": "remote", "monitoring_state": "active"}, {}, {}) == 4,
)
check(
    "rank.row_fallback.unread",
    main._session_status_rank({"id": "remote", "unread_count": 5}, {}, {}) == 2,
)

# ── 2. list sort key (non-search): empty > pinned > status > ts ───────────
def lkey(sess):
    return main._session_list_sort_key(
        sess, False, "updated_at",
        status_sort=True, monitoring_by_sid=mon, unread_by_sid=unread,
    )


running_old = {"id": "run", "updated_at": "2020-01-01T00:00:00", "message_count": 5}
idle_new = {"id": "idle", "updated_at": "2030-01-01T00:00:00", "message_count": 5}
# running (rank 4) sorts above idle even though idle is far newer (reverse=True)
check("listkey.status_beats_time", lkey(running_old) > lkey(idle_new))

pinned_idle = {"id": "idle", "updated_at": "2020-01-01T00:00:00", "message_count": 5, "pinned": True}
# pinned (idle) beats unpinned running → pinned ABOVE status
check("listkey.pinned_beats_status", lkey(pinned_idle) > lkey(running_old))

empty_new = {"id": "idle", "updated_at": "2031-01-01T00:00:00", "message_count": 0}
# empty-new session beats even a pinned running one → empty ABOVE all
pinned_running = {"id": "run", "updated_at": "2020-01-01T00:00:00", "message_count": 5, "pinned": True}
check("listkey.empty_beats_all", lkey(empty_new) > lkey(pinned_running))

# status_sort=False → identical to the legacy (isEmpty, pinned, ts) shape
off = main._session_list_sort_key(running_old, False, "updated_at")
legacy_ts = main.session_store.timestamp_sort_value("2020-01-01T00:00:00")
check(
    "listkey.off_is_legacy_shape",
    len(off) == 3 and off == (False, False, legacy_ts),
)

# ── 3. filtered (search) key: relevance dominates, status below score ─────
scores = {"hi": 10, "lo": 1}

def fkey(sess):
    return main._session_filtered_sort_key(
        sess, folder_view=False, search="q", content_scores=scores,
        sort_by="updated_at", status_sort=True,
        monitoring_by_sid=mon, unread_by_sid=unread,
    )


hi_idle = {"id": "hi", "updated_at": "2020-01-01T00:00:00"}
lo_running = {"id": "lo", "updated_at": "2020-01-01T00:00:00"}
# higher search score wins even though the other is running → status BELOW score
# (lo_running's id isn't in `mon` as running; give it running via markers)
lo_running["markers"] = marker(NEEDS)
check("searchkey.relevance_dominates", fkey(hi_idle) > fkey(lo_running))
# within equal score, status breaks the tie above ts
a = {"id": "hi", "updated_at": "2020-01-01T00:00:00", "markers": marker(NEEDS)}
b = {"id": "hi", "updated_at": "2025-01-01T00:00:00"}
check("searchkey.status_tiebreak_within_score", fkey(a) > fkey(b))


if failures:
    print(f"\n{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("\nPASS test_status_sort_rank")
