#!/usr/bin/env python3
"""Locks two contracts about the pending-interactions bulk lookup added in
be912e056 ("perf(sessions): kill per-record full-store deepcopy in
list_sessions — 52.9x"), which shipped that commit's "batched the
pending-interactions N+1 (4 sub-store bulk counterparts)" claim with no
dedicated regression test — this file is that test, sibling to
`test_session_organization_bulk_lookup.py` (same isolation/counting idiom,
different subsystem).

The 4 legacy pending-interaction mechanisms and their bulk counterparts,
tied together by `backend.adapters.store_access.StoreAccess.
list_pending_interactions_for_sessions` (single-id counterpart:
`list_pending_interactions`):

  1. `tool_approval.ToolApprovalRegistry.grouped_by_session` (single-id:
     `list_for_session`)
  2. `stores.pending_approvals.list_pending` (already whole-store; grouped
     by `app_session_id` inline in `list_pending_interactions_for_sessions`)
  3. `session_bridge.pending_by_caller` (single-id: `list_pending_for_caller`)
  4. `user_input_store.pending_for_sessions` (single-id: `pending_for_session`)

`adapters/session_adapter.py::SessionSurfaceAdapter.list_sessions` calls
`store_access.list_pending_interactions_for_sessions(page_ids)` exactly
ONCE per page — never `list_pending_interactions(r.id)` looped per record
(that per-record loop is the exact N+1 shape this test guards against; see
`_toggle_page_loop_to_per_record` below, used only by the failing-first
proof, never by the tests themselves).

Covers:
  (a) EQUIVALENCE — the bulk lookup, called once for N session ids, returns
      exactly what looping the single-id lookup over the same N ids and
      merging would return (deep-equal, keyed by session id), across mixed
      pending/empty sessions and all 4 mechanisms.
  (b) N+1 GUARD — `store_access.list_pending_interactions_for_sessions` is
      invoked EXACTLY ONCE per `SessionSurfaceAdapter.list_sessions()` call,
      regardless of page size or how many sessions are in that page (i.e.
      NOT once per session).

Run with:
    cd backend && .venv/bin/python scripts/test_pending_interactions_bulk_lookup.py
    PYTHONPATH=.:./backend python3 -m pytest backend/scripts/test_pending_interactions_bulk_lookup.py -q
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import uuid

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-pending-interactions-bulk-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
_REPO_ROOT = os.path.dirname(_BACKEND)
for _p in (_BACKEND, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import session_bridge  # noqa: E402
import session_store  # noqa: E402
import tool_approval  # noqa: E402
import user_input_store  # noqa: E402
from stores import pending_approvals  # noqa: E402

from backend.adapters import session_adapter as session_adapter_mod  # noqa: E402
from backend.adapters.session_adapter import SessionSurfaceAdapter  # noqa: E402
from backend.adapters.store_access import store_access  # noqa: E402
from backend.surface_contract.identity import Ok  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


# ---------------------------------------------------------------------------
# Seeding helpers — one per legacy mechanism, using each store's own public
# write API (matching the bare-import convention of the rest of
# backend/scripts).
# ---------------------------------------------------------------------------

def _seed_tool_approval(session_id: str) -> None:
    async def _create() -> None:
        tool_approval.registry.create(
            app_session_id=session_id, run_id="run-1", provider_kind="claude",
            tool_name="bash", summary={"command": "ls"},
        )
    asyncio.run(_create())


def _seed_worker_approval(session_id: str) -> None:
    pending_approvals.create(
        delegation_id=f"deleg-{uuid.uuid4().hex}", app_session_id=session_id,
        cwd="/tmp", justification="test", proposed_description="do the thing",
        proposed_orchestration_mode="native", instructions_preview="preview",
        model="m",
    )


def _seed_delegate_choice(session_id: str) -> None:
    did = f"deleg-{uuid.uuid4().hex}"
    session_bridge._pending[did] = {
        "future": None, "caller_sid": session_id, "caller_msg_id": "msg-1",
        "target_sid": None, "prompt": "pick one", "run_mode": "fork",
        "proposed_ids": ["a", "b"],
    }


def _seed_user_input(session_id: str) -> None:
    user_input_store.create_request(
        app_session_id=session_id, kind="input",
        questions=[{"id": "q1", "text": "which?"}], prompt="pick",
        timeout_seconds=None,
    )


def _session_id(name: str) -> str:
    cwd = f"/tmp/ba-pending-bulk-{uuid.uuid4().hex}"
    os.makedirs(cwd, exist_ok=True)
    return session_store.create_session(name=name, cwd=cwd)["id"]


def _adapter() -> SessionSurfaceAdapter:
    adapter = SessionSurfaceAdapter()
    adapter.bind()
    return adapter


def _unwrap(result):
    assert isinstance(result, Ok), result
    return result.value


# ---------------------------------------------------------------------------
# (a) EQUIVALENCE — bulk lookup for N ids == merged single-id lookups.
# ---------------------------------------------------------------------------

def _check_equivalence() -> tuple[bool, str]:
    sid_tool = f"sess-tool-{uuid.uuid4().hex}"
    sid_worker = f"sess-worker-{uuid.uuid4().hex}"
    sid_delegate = f"sess-delegate-{uuid.uuid4().hex}"
    sid_input = f"sess-input-{uuid.uuid4().hex}"
    sid_empty = f"sess-empty-{uuid.uuid4().hex}"
    sid_all = f"sess-all-{uuid.uuid4().hex}"

    _seed_tool_approval(sid_tool)
    _seed_worker_approval(sid_worker)
    _seed_delegate_choice(sid_delegate)
    _seed_user_input(sid_input)
    # sid_empty deliberately gets nothing.
    _seed_tool_approval(sid_all)
    _seed_worker_approval(sid_all)
    _seed_delegate_choice(sid_all)
    _seed_user_input(sid_all)

    ids = [sid_tool, sid_worker, sid_delegate, sid_input, sid_empty, sid_all]

    bulk = store_access.list_pending_interactions_for_sessions(ids)
    merged = {sid: store_access.list_pending_interactions(sid) for sid in ids}

    if set(bulk.keys()) != set(ids):
        return False, f"bulk keys {sorted(bulk.keys())} != requested ids {sorted(ids)}"
    for sid in ids:
        if bulk[sid] != merged[sid]:
            return False, f"sid={sid}: bulk={bulk[sid]!r} != single-id merged={merged[sid]!r}"
    if bulk[sid_empty] != ():
        return False, f"sid_empty should have no pending interactions, got {bulk[sid_empty]!r}"
    if len(bulk[sid_all]) != 4:
        return False, f"sid_all should have all 4 mechanisms pending, got {bulk[sid_all]!r}"
    return True, ""


# ---------------------------------------------------------------------------
# (b) N+1 GUARD — list_pending_interactions_for_sessions called exactly
# once per SessionSurfaceAdapter.list_sessions() call, not once per session.
# ---------------------------------------------------------------------------

def _count_bulk_lookup_calls():
    """Wraps `store_access.list_pending_interactions_for_sessions` (an
    instance attribute shadow, same idiom as `_count_loads` in
    `test_session_organization_bulk_lookup.py`) with a call counter.
    Returns (counter_box, restore_fn)."""
    calls = [0]
    original = store_access.list_pending_interactions_for_sessions

    def _counting(session_ids):
        calls[0] += 1
        return original(session_ids)

    store_access.list_pending_interactions_for_sessions = _counting
    return calls, lambda: setattr(
        store_access, "list_pending_interactions_for_sessions", original,
    )


def _walk_all_pages(adapter: SessionSurfaceAdapter) -> tuple[list, int]:
    """Pages `list_sessions()` to exhaustion. Returns (all_sessions,
    number_of_list_sessions_calls)."""
    sessions: list = []
    cursor = None
    n_calls = 0
    for _ in range(50):  # bounded walk
        page = _unwrap(adapter.list_sessions(cursor, None))
        n_calls += 1
        sessions.extend(page.sessions)
        cursor = page.next_cursor
        if cursor is None:
            break
    return sessions, n_calls


def _check_n_plus_1_guard_for_page_size(
    n_sessions: int, page_size: int,
) -> tuple[bool, str]:
    session_ids = [_session_id(f"pending-bulk-{i}") for i in range(n_sessions)]
    for i, sid in enumerate(session_ids):
        if i % 2 == 0:
            _seed_tool_approval(sid)

    original_page_size = session_adapter_mod._PAGE_SIZE
    session_adapter_mod._PAGE_SIZE = page_size
    calls, restore = _count_bulk_lookup_calls()
    try:
        adapter = _adapter()
        sessions, n_list_sessions_calls = _walk_all_pages(adapter)
    finally:
        restore()
        session_adapter_mod._PAGE_SIZE = original_page_size

    seen_ids = {s.session_id for s in sessions}
    if not set(session_ids).issubset(seen_ids):
        return False, f"missing seeded sessions from paged listing: {set(session_ids) - seen_ids}"
    if calls[0] != n_list_sessions_calls:
        return False, (
            f"page_size={page_size}, n_sessions={n_sessions}: "
            f"list_pending_interactions_for_sessions called {calls[0]} times "
            f"across {n_list_sessions_calls} list_sessions() calls (expected "
            f"exactly one bulk call per list_sessions() call, NOT one per "
            f"session — an N+1 regression here would show a count scaling "
            f"with n_sessions instead)"
        )
    return True, ""


def _run() -> bool:
    results: list[tuple[str, bool, str]] = []

    ok, msg = _check_equivalence()
    results.append((
        "list_pending_interactions_for_sessions(N ids) == merged "
        "list_pending_interactions(id) per id, across all 4 mechanisms",
        ok, msg,
    ))

    # page_size=2 over 5 sessions -> 3 pages (2, 2, 1): bulk-lookup call
    # count must track PAGES (3), never SESSIONS (5).
    ok, msg = _check_n_plus_1_guard_for_page_size(n_sessions=5, page_size=2)
    results.append((
        "list_sessions (page_size=2, 5 sessions, 3 pages): bulk lookup "
        "called exactly once per list_sessions() call",
        ok, msg,
    ))

    # All sessions fit in a single page -> exactly 1 list_sessions() call,
    # exactly 1 bulk-lookup call.
    ok, msg = _check_n_plus_1_guard_for_page_size(n_sessions=6, page_size=50)
    results.append((
        "list_sessions (page_size=50, 6 sessions, 1 page): bulk lookup "
        "called exactly once",
        ok, msg,
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


def test_pending_interactions_bulk_lookup_matches_and_is_o1_per_page() -> None:
    """pytest entry point — `_run()` prints its own PASS/FAIL diagnostics
    either way, matching `test_session_organization_bulk_lookup.py`."""
    assert _run()


if __name__ == "__main__":
    sys.exit(main())
