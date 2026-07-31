"""Session status dimension + project aggregation regression tests.

Locks the single source of truth for the five independent session status
dimensions and the two projections built on it:

  1. `session_status.compute` derives Running / Waiting-on-user / Unread
     / Errored / Is-done independently — dimensions combine freely and
     never mask one another, across the full 3x2x2x2x2 combination
     space.
  2. `session_listing_api._session_status_key` is a pure priority
     projection of those dimensions (the sidebar bucket).
  3. `projects_api._project_aggregates` counts each dimension
     independently, from the SAME derivation the rows use, so a project
     counter can never disagree with the rows it summarizes. In
     particular it reads the monitoring snapshot, not process liveness:
     a live-but-idle or approval-blocked session is not "running".

Run with:
    cd backend && .venv/bin/python scripts/test_session_status_dimensions.py
"""
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home  # noqa: E402
_test_home.isolate("bc_test_status_dims_")

import projects_api  # noqa: E402
import session_listing_api  # noqa: E402
import session_status  # noqa: E402
import user_input_store  # noqa: E402
import working_mode  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    if not cond:
        failures.append(name)
        print(f"FAIL {name}")
    else:
        print(f"ok   {name}")


NEEDS = session_status.MARKER_TAG_NEEDS_DECISION
DONE = session_status.MARKER_TAG_ALL_TASKS_DONE

#: (monitoring_state, expected Running dimension). "idle" and
#: "blocked_on_user" are live processes with NO work in flight, so both
#: read as IDLE on the Running dimension.
RUNNING_CASES = [
    ("active", session_status.RUNNING),
    ("waiting_on_background", session_status.AWAITING_BACKGROUND),
    ("idle", session_status.IDLE),
    ("stopped", session_status.IDLE),
    ("blocked_on_user", session_status.IDLE),
]


# ── 1. Running dimension mapping ─────────────────────────────────────────
for state, expected in RUNNING_CASES:
    check(
        f"running.{state}",
        session_status.running_dimension(state) == expected,
    )
check("running.unknown_is_idle", session_status.running_dimension(None) == session_status.IDLE)
check(
    "running.busy_only_for_work",
    [
        session_status.compute({"id": "s"}, {"s": st}, {}).busy
        for st, _ in RUNNING_CASES
    ]
    == [True, True, False, False, False],
)


# ── 2. Every dimension combination is independent ────────────────────────
def build(state, waiting, unread, errored, done) -> tuple[dict, dict, dict]:
    """A session row + snapshots expressing exactly one combination."""
    markers = {}
    if waiting:
        markers["ext-a"] = {"color": "#f00", "tooltip": "t", "tag": NEEDS}
    if done:
        markers["ext-b"] = {"color": "#00f", "tooltip": "t", "tag": DONE}
    session = {"id": "s", "markers": markers}
    if errored:
        session["unseen_error"] = {"msg": "boom"}
    return session, {"s": state}, {"s": 3 if unread else 0}


COMBOS = list(itertools.product(
    [state for state, _ in RUNNING_CASES], [False, True], [False, True],
    [False, True], [False, True],
))

mismatched: list[str] = []
for state, waiting, unread, errored, done in COMBOS:
    session, mon, unr = build(state, waiting, unread, errored, done)
    st = session_status.compute(session, mon, unr)
    # blocked_on_user sets waiting-on-user on its own; it can never be
    # false for that state.
    expect_waiting = waiting or state == "blocked_on_user"
    if (
        st.running != dict(RUNNING_CASES)[state]
        or st.waiting_for_user != expect_waiting
        or st.unread != unread
        or st.errored != errored
        or st.is_done != done
    ):
        mismatched.append(f"{state}/{waiting}/{unread}/{errored}/{done} -> {st}")

check(f"dimensions.independent.{len(COMBOS)}_combinations", not mismatched)
if mismatched:
    for m in mismatched[:8]:
        print("   ", m)

check(
    "dimensions.errored_and_unread_coexist",
    (lambda s: s.errored and s.unread)(
        session_status.compute(
            {"id": "s", "unseen_error": {"m": 1}}, {"s": "stopped"}, {"s": 2}
        )
    ),
)
check(
    "dimensions.done_and_waiting_coexist",
    (lambda s: s.is_done and s.waiting_for_user)(
        session_status.compute(
            {
                "id": "s",
                "markers": {
                    "a": {"tag": NEEDS, "color": "#f00", "tooltip": "t"},
                    "b": {"tag": DONE, "color": "#00f", "tooltip": "t"},
                },
            },
            {"s": "stopped"},
            {},
        )
    ),
)
check(
    "dimensions.pending_input_snapshot_sets_waiting",
    session_status.compute({"id": "s"}, {"s": "idle"}, {}, {"s": 1}).waiting_for_user,
)
check(
    "dimensions.pending_input_row_fallback",
    session_status.compute(
        {"id": "s", "pending_user_input_count": 2}, {"s": "idle"}, {}
    ).waiting_for_user,
)
check(
    "dimensions.remote_row_monitoring_fallback",
    session_status.compute(
        {"id": "remote", "monitoring_state": "active"}, {}, {}
    ).running == session_status.RUNNING,
)


# ── 3. Bucket projection still matches the locked priority order ─────────
def bucket(session, mon, unread, pending=None) -> str:
    return session_listing_api._session_status_key(session, mon, unread, pending)


check("bucket.error_wins", bucket(
    {"id": "s", "unseen_error": {"m": 1}, "markers": {"a": {"tag": NEEDS}}},
    {"s": "active"}, {"s": 5},
) == "error")
check("bucket.needs_decision_over_unread", bucket(
    {"id": "s", "markers": {"a": {"tag": NEEDS}}}, {"s": "stopped"}, {"s": 5},
) == "needs_decision")
check("bucket.blocked_on_user_is_needs_decision", bucket(
    {"id": "s"}, {"s": "blocked_on_user"}, {},
) == "needs_decision")
check("bucket.unread_only_when_not_busy", bucket(
    {"id": "s"}, {"s": "active"}, {"s": 5},
) == "running")
check("bucket.unread_when_idle", bucket(
    {"id": "s"}, {"s": "idle"}, {"s": 5},
) == "unread")
check("bucket.open_work_over_running", bucket(
    {"id": "s", "current_todos": [{"status": "pending"}]}, {"s": "active"}, {},
) == "open_work")
check("bucket.background_counts_as_running", bucket(
    {"id": "s"}, {"s": "waiting_on_background"}, {},
) == "running")
check("bucket.all_done", bucket(
    {"id": "s", "markers": {"a": {"tag": DONE}}}, {"s": "stopped"}, {},
) == "all_done")
check("bucket.idle", bucket({"id": "s"}, {"s": "stopped"}, {}) == "idle")


# ── 4. Project aggregation counts every dimension, from monitoring ───────
PROJECT = "/tmp/proj-a"
OTHER = "/tmp/proj-b"

SESSIONS = [
    # running (active) + unread
    {"id": "s-run", "cwd": PROJECT, "unread_count": 0},
    # background work still finishing — NOT counted as running
    {"id": "s-bg", "cwd": PROJECT},
    # live process, no work in flight — NOT counted as running
    {"id": "s-idle-alive", "cwd": PROJECT},
    # blocked on an approval — waiting, NOT running
    {"id": "s-blocked", "cwd": PROJECT},
    # errored AND unread at once — increments both counters
    {"id": "s-err", "cwd": PROJECT, "unseen_error": {"m": 1}},
    # NEEDS_USER_DECISION marker while idle
    {"id": "s-marker", "cwd": PROJECT, "markers": {"a": {"tag": NEEDS}}},
    # different project entirely
    {"id": "s-other", "cwd": OTHER},
]
MONITORING = {
    "s-run": "active",
    "s-bg": "waiting_on_background",
    "s-idle-alive": "idle",
    "s-blocked": "blocked_on_user",
    "s-err": "stopped",
    "s-marker": "stopped",
    "s-other": "active",
}
UNREAD = {"s-run": 2, "s-err": 4}
PENDING = {}

_orig = {
    "list": session_manager.list,
    "unread": session_manager.unread_counts_snapshot,
    "pending": user_input_store.pending_counts_by_session,
    "hide": working_mode.should_hide_from_sidebar,
}
session_manager.list = lambda *a, **k: [dict(s) for s in SESSIONS]
session_manager.unread_counts_snapshot = lambda: dict(UNREAD)
user_input_store.pending_counts_by_session = lambda: dict(PENDING)
working_mode.should_hide_from_sidebar = lambda s: False
projects_api.configure(
    notify_projects_changed=lambda: None,
    broadcast_global=lambda *a, **k: None,
    # (running_sids, monitoring_by_sid) — running_sids is process
    # liveness and deliberately WRONG here: every session looks alive.
    # The aggregation must ignore it and read monitoring instead.
    cached_state_snapshot=lambda: (set(MONITORING), dict(MONITORING)),
)
try:
    projects_api.invalidate_project_aggregates()
    aggs = projects_api._project_aggregates()
    slot = aggs.get((PROJECT, "primary"), {})
    check("aggregate.running_excludes_idle_and_blocked", slot.get("running_count") == 1)
    check("aggregate.unread_counts_sessions", slot.get("unread_session_count") == 2)
    check("aggregate.waiting_counts_blocked_and_marker", slot.get("waiting_for_user_count") == 2)
    check("aggregate.errored", slot.get("errored_count") == 1)
    check(
        "aggregate.dimensions_do_not_mask",
        slot.get("errored_count") == 1 and slot.get("unread_session_count") == 2,
    )
    check(
        "aggregate.scoped_per_project",
        aggs.get((OTHER, "primary"), {}).get("running_count") == 1,
    )
    check(
        "aggregate.empty_shape_matches",
        set(projects_api.empty_aggregate()) == set(slot),
    )

    # A pending user-input request must move the counter with no session
    # row change — the aggregation reads the live snapshot.
    PENDING["s-run"] = 1
    projects_api.invalidate_project_aggregates()
    slot2 = projects_api._project_aggregates().get((PROJECT, "primary"), {})
    check("aggregate.pending_input_moves_waiting", slot2.get("waiting_for_user_count") == 3)
    check("aggregate.running_unaffected_by_waiting", slot2.get("running_count") == 1)
finally:
    session_manager.list = _orig["list"]
    session_manager.unread_counts_snapshot = _orig["unread"]
    user_input_store.pending_counts_by_session = _orig["pending"]
    working_mode.should_hide_from_sidebar = _orig["hide"]
    projects_api.invalidate_project_aggregates()


print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all status dimension checks passed")
