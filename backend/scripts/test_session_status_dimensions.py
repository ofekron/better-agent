"""Canonical session status dimension regression tests.

Locks the single source of truth for the five independent session status
dimensions and its sidebar projection:

  1. `session_status.compute` derives Running / Waiting-on-user / Unread
     / Errored / Is-done independently — dimensions combine freely and
     never mask one another, across the full 3x2x2x2x2 combination
     space.
  2. `session_status.project` is the pure priority
     projection of those dimensions (the sidebar bucket).
Run with:
    ./scripts/run-backend-tests.sh -- scripts/test_session_status_dimensions.py
"""
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home  # noqa: E402
_test_home.isolate("bc_test_status_dims_")

import session_status  # noqa: E402

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
# A corrupt pending count (non-int string, or a non-numeric container) is
# defended against: it reads as 0 rather than crashing compute.
check(
    "dimensions.corrupt_pending_snapshot_reads_zero",
    session_status._pending_input_count({"id": "s"}, {"s": "not-a-number"}) == 0,
)
check(
    "dimensions.non_int_pending_type_reads_zero",
    session_status._pending_input_count({"id": "s"}, {"s": [1, 2]}) == 0,
)
check(
    "dimensions.remote_row_monitoring_fallback",
    session_status.compute(
        {"id": "remote", "monitoring_state": "active"}, {}, {}
    ).running == session_status.RUNNING,
)


# ── 3. Bucket projection still matches the locked priority order ─────────
def bucket(session, mon, unread, pending=None) -> str:
    return session_status.project(session, mon, unread, pending)["status_key"]


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


print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all status dimension checks passed")


def test_session_status_dimensions() -> None:
    assert failures == []
