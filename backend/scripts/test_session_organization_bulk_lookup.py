"""Locks the contract that resolving session-organization data for MANY
sessions at once costs exactly one full-store `_load()` (which does a
recursive `copy.deepcopy` of every folder/tag/assignment in
`session_organization.json`), not one `_load()` per session.

Incident 2026-08-08: the live backend's event loop repeatedly stalled for
seconds at a time (`lag_watchdog` "process CPU/GIL starvation candidate",
loop lag up to 15s). A `faulthandler` dump taken mid-stall landed, on every
single sample across 37 separate dumps, inside the exact same synchronous
stack:

    adapter_api.py:list_sessions
    -> adapters/session_adapter.py:SessionSurfaceAdapter.list_sessions
    -> adapters/store_access.py:get_session_organization  (per record)
    -> session_organization_store.py:organization_for_session
    -> session_organization_store.py:_load -> copy.deepcopy(...)

`SessionSurfaceAdapter.list_sessions` called the single-id
`store_access.get_session_organization(r.id)` once per record in its page
(up to `_PAGE_SIZE`=50), and `_matches_filters` did the same across every
candidate record pre-paging. Each call deep-copied the ENTIRE
session_organization store (folders + tags + assignments for every
session/project, ~500KB / 2300+ assignments on the live instance) — an
O(N) full-store copy per request, run synchronously on the event loop
thread with no `await` boundary, for a REST route the frontend polls
continuously.

Fix: `session_organization_store.organizations_for_sessions(session_ids)`
and `store_access.get_session_organizations(session_ids)` load/deep-copy
the store ONCE per call regardless of how many ids are requested;
`SessionSurfaceAdapter.list_sessions`/`_matches_filters` now call the bulk
form exactly once per page/filter pass instead of looping the single-id
call. This test proves the O(1)-per-call property directly against the
store (would AttributeError pre-fix — `organizations_for_sessions` did not
exist) and proves the per-id path is untouched (still N calls when a
caller genuinely wants one-at-a-time).

Run with:
    cd backend && .venv/bin/python scripts/test_session_organization_bulk_lookup.py
"""

from __future__ import annotations

import os
import shutil
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-org-bulk-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_organization_store as sos  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _seed(n_sessions: int, n_tags: int) -> tuple[list[str], list[str]]:
    tag_ids = [sos.create_tag(name=f"tag-{i}", project_id=None)["id"] for i in range(n_tags)]
    session_ids = [f"sess-{i}" for i in range(n_sessions)]
    for i, sid in enumerate(session_ids):
        sos.set_session_tags(sid, [tag_ids[i % n_tags]], source="manual")
    return session_ids, tag_ids


def _count_loads() -> tuple[list[int], callable]:
    """Wraps `session_organization_store._load` with a call counter,
    returns (counter_box, restore_fn)."""
    calls = [0]
    original = sos._load

    def _counting_load():
        calls[0] += 1
        return original()

    sos._load = _counting_load
    return calls, lambda: setattr(sos, "_load", original)


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []

    session_ids, _tag_ids = _seed(n_sessions=50, n_tags=5)

    # 1. Bulk lookup of all 50 sessions costs exactly ONE `_load()` call —
    #    the actual regression: pre-fix there was no bulk API, so N
    #    sessions meant N `_load()`/full-store-deepcopy calls.
    calls, restore = _count_loads()
    try:
        orgs = sos.organizations_for_sessions(session_ids)
    finally:
        restore()
    results.append((
        "organizations_for_sessions(50 ids) calls _load() exactly once",
        calls[0] == 1,
        f"got {calls[0]} _load() calls for 50 ids",
    ))
    results.append((
        "organizations_for_sessions returns an entry per requested id",
        len(orgs) == len(session_ids) and all(sid in orgs for sid in session_ids),
        f"got {len(orgs)} entries for {len(session_ids)} ids",
    ))

    # 2. Each bulk entry matches the single-id `organization_for_session`
    #    result exactly (same shape, same values) — the batched path must
    #    not change behavior, only call count.
    sample_sid = session_ids[7]
    single = sos.organization_for_session(sample_sid)
    bulk = orgs[sample_sid]
    results.append((
        "bulk entry matches organization_for_session for the same id",
        single == bulk,
        f"single={single!r} bulk={bulk!r}",
    ))

    # 3. The single-id path is untouched: N separate calls still cost N
    #    `_load()` calls (proves the fix batches at the CALLER'S choice,
    #    not by secretly caching across unrelated single-id calls, which
    #    would risk staleness).
    calls2, restore2 = _count_loads()
    try:
        for sid in session_ids[:5]:
            sos.organization_for_session(sid)
    finally:
        restore2()
    results.append((
        "organization_for_session still costs one _load() per call",
        calls2[0] == 5,
        f"got {calls2[0]} _load() calls for 5 single-id calls",
    ))

    # 4. Empty input costs zero extra work beyond the one _load() (no
    #    per-id short-circuit skipped it entirely — still O(1)).
    calls3, restore3 = _count_loads()
    try:
        empty = sos.organizations_for_sessions([])
    finally:
        restore3()
    results.append((
        "organizations_for_sessions([]) still calls _load() at most once",
        calls3[0] <= 1 and empty == {},
        f"got {calls3[0]} _load() calls, result={empty!r}",
    ))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' — ' + msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


def main() -> int:
    try:
        ok = _run()
        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


def test_session_organization_bulk_lookup_is_o1_per_call() -> None:
    """pytest entry point — `run-backend-tests.sh` collects this; `_run()`
    prints its own PASS/FAIL diagnostics either way."""
    assert _run()


if __name__ == "__main__":
    sys.exit(main())
