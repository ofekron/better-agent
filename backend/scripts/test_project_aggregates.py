"""Locks the per-project aggregate enrichment computed by
`projects_api._project_aggregates`:

1. Two user-kind sessions running in the same cwd → running_count=2.
2. After one completes, running_count=1.
3. Two sessions with unread messages → unread_session_count=2.
4. Worker forks (`delegate_fork`, etc.) are excluded — they don't
   inflate either count.
5. `/api/sessions` enrichment carries `is_running` + `unread_count`
   per row; sidebar consumers read it directly.

Run with:
    cd backend && .venv/bin/python scripts/test_project_aggregates.py
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-projagg-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_installation  # noqa: E402

_test_installation.activate(Path(_TMP_HOME))

from starlette.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

# Import after the env tempdir is set — main.py wires the coordinator
# singleton at import time.
import main as backend_main  # noqa: E402
import projects_api  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


CWD = "/tmp/test-projagg"


def _mk_session() -> str:
    sess = session_manager.create(
        name="t", model="sonnet", cwd=CWD,
        orchestration_mode="native", source="cli",
    )
    return sess["id"]


def _native_event(uuid: str) -> dict:
    return {
        "type": "agent_message",
        "data": {
            "uuid": uuid,
            "type": "assistant",
            "message": {"content": "x"},
        },
    }


def test_running_count_aggregation() -> None:
    s1 = _mk_session()
    s2 = _mk_session()
    s3 = _mk_session()
    coord = backend_main.coordinator
    # Simulate production flow: active_run_ids is set before run_state_add
    # so _prune_dead_entries sees the entries as managed (not orphaned).
    coord.active_run_ids[s1] = ["r1"]
    coord.active_run_ids[s2] = ["r2"]
    coord.run_state_add(s1, run_id="r1", kind="native", target_message_id=None)
    coord.run_state_add(s2, run_id="r2", kind="native", target_message_id=None)
    coord.turn_manager._refresh_cache()
    projects_api.invalidate_project_aggregates()

    aggs = projects_api._project_aggregates()
    key = (CWD, "primary")
    assert key in aggs, f"project {CWD} missing from aggs: {aggs}"
    rc = aggs[key]["running_count"]
    assert rc == 2, f"expected running_count=2 ({s1},{s2} running; {s3} idle), got {rc}"

    coord.run_state_remove(s1, "r1")
    coord.turn_manager._refresh_cache()
    projects_api.invalidate_project_aggregates()
    aggs = projects_api._project_aggregates()
    rc = aggs[key]["running_count"]
    assert rc == 1, f"after one completes expected 1, got {rc}"
    coord.run_state_remove(s2, "r2")
    print(f"{PASS} running_count_aggregation")


def test_unread_session_count_aggregation() -> None:
    s1 = _mk_session()
    s2 = _mk_session()
    # Append assistant scaffolds + 2 events on s1, 3 on s2.
    for sid, n in [(s1, 2), (s2, 3)]:
        strategy = get_strategy("native")
        scaffold = strategy.build_assistant_scaffold()
        session_manager.append_assistant_msg(sid, scaffold)
        msg_ref = session_manager._cached(sid)["messages"][-1]
        ctx = ApplyEventCtx(root_id=sid)
        for i in range(n):
            strategy.apply_event(
                app_session_id=sid, msg=msg_ref,
                event=_native_event(f"{sid[:4]}-{i}"),
                ctx=ctx, source_is_provider_stream=False,
            )
        session_manager.warm_unread(sid)
    projects_api.invalidate_project_aggregates()

    aggs = projects_api._project_aggregates()
    key = (CWD, "primary")
    total = aggs[key]["unread_session_count"]
    assert total == 2, f"expected unread_session_count=2, got {total}"
    print(f"{PASS} unread_session_count_aggregation")


def test_worker_fork_excluded_from_aggregates() -> None:
    """A delegate_fork lives embedded in its parent's tree — it's NOT
    a sidebar root, so `session_manager.list()` already filters it out.
    Result: no matter what state the worker fork is in, it can't leak
    into the project aggregate."""
    root = _mk_session()
    fork = session_manager.create_delegate_fork(
        parent_agent_session_id=root,
        caller_agent_session_id=root,
        parent_agent_sid_at_fork="fake-sid",
        parent_line_count_at_fork=0,
        orchestration_mode="native",
    )
    session_manager._roots.pop(root, None)

    # Run on the fork — running flag stays off at the user level by
    # design (mutator filter), so the aggregate shouldn't see it.
    coord = backend_main.coordinator
    coord.active_run_ids[fork["id"]] = ["rw"]
    coord.run_state_add(fork["id"], run_id="rw", kind="worker", target_message_id=None)
    coord.turn_manager._refresh_cache()
    projects_api.invalidate_project_aggregates()

    aggs = projects_api._project_aggregates()
    key = (CWD, "primary")
    # Only the root session counts. It's NOT running (we didn't
    # run_state_add on the root sid), so running_count is 0.
    rc = aggs.get(key, {"running_count": 0})["running_count"]
    assert rc == 0, (
        f"worker fork must not inflate project running_count; got {rc}"
    )
    coord.run_state_remove(fork["id"], "rw")
    print(f"{PASS} worker_fork_excluded_from_aggregates")


def _sessions_page(accept_encoding: str) -> dict:
    """Fetch `/api/sessions` over the real ASGI stack. The endpoint returns a
    gzip-negotiated `Response`, not a dict, and its query params are FastAPI
    `Query` defaults — so the sidebar payload is only reachable through an
    actual request."""
    client = TestClient(
        backend_main.app,
        client=("127.0.0.1", 50000),
        base_url="http://localhost:8000",
    )
    client.headers.update({
        "Authorization": f"Bearer {auth.create_token('native')}",
        "Accept-Encoding": accept_encoding,
    })
    resp = client.get("/api/sessions", params={"limit": 200, "project_path": CWD})
    assert resp.status_code == 200, f"/api/sessions -> {resp.status_code}: {resp.text}"
    encoded = resp.headers.get("content-encoding", "")
    if accept_encoding == "identity":
        assert encoded != "gzip", "identity request must not be gzip-encoded"
    return resp.json()


def _enriched_row(payload: dict, sid: str) -> dict:
    rows = payload["sessions"]
    row = next((r for r in rows if r.get("id") == sid), None)
    assert row is not None, f"session {sid} missing from /api/sessions output"
    return row


# The contract this test exists to protect: the sidebar-enrichment fields
# `get_sessions` decorates onto every row. Timestamps and other summary
# fields are deliberately NOT part of it — `session_store.list_sessions`
# documents that the summary index may lag in-memory mutations by up to
# `PERSIST_DEBOUNCE_S`, so comparing whole rows across two requests races
# that window.
_ENRICHMENT_FIELDS = ("is_running", "unread_count")


def _enrichment(row: dict) -> dict:
    return {field: row.get(field) for field in _ENRICHMENT_FIELDS}


def test_session_list_enrichment() -> None:
    """The `/api/sessions` enrichment carries `is_running` +
    `unread_count` per row. Mirrors the sidebar's render path."""
    sid = _mk_session()
    coord = backend_main.coordinator
    coord.active_run_ids[sid] = ["rr"]
    coord.run_state_add(sid, run_id="rr", kind="native", target_message_id=None)
    coord.turn_manager._refresh_cache()
    projects_api.invalidate_project_aggregates()
    # Force one event so unread > 0.
    strategy = get_strategy("native")
    scaffold = strategy.build_assistant_scaffold()
    session_manager.append_assistant_msg(sid, scaffold)
    msg_ref = session_manager._cached(sid)["messages"][-1]
    ctx = ApplyEventCtx(root_id=sid)
    strategy.apply_event(
        app_session_id=sid, msg=msg_ref,
        event=_native_event("enrich-u"),
        ctx=ctx, source_is_provider_stream=False,
    )
    session_manager.warm_unread(sid)

    expected = {"is_running": True, "unread_count": 1}
    # `request` only carries Accept-Encoding into this endpoint (transport
    # encoding + response-cache key), so the enrichment fields must be
    # identical on both encodings.
    for accept_encoding in ("gzip", "identity"):
        row = _enriched_row(_sessions_page(accept_encoding), sid)
        assert _enrichment(row) == expected, (
            f"enrichment for accept-encoding={accept_encoding} expected "
            f"{expected}, got {_enrichment(row)}"
        )
    coord.run_state_remove(sid, "rr")
    print(f"{PASS} session_list_enrichment")


def main() -> int:
    try:
        test_running_count_aggregation()
        test_unread_session_count_aggregation()
        test_worker_fork_excluded_from_aggregates()
        test_session_list_enrichment()
        print("ALL PASSED")
        return 0
    except AssertionError as e:
        print(f"{FAIL}: {e}")
        return 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
