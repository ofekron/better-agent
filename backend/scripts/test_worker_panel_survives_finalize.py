"""Regression for the reconcile-elimination worker-panel-wipe bug.

`msg["workers"]` is owned exclusively by the fold: `worker_start` /
`worker_complete` / `worker_event` facts write it via
`session_manager.upsert_worker_panel` / `update_worker_panel` /
`apply_worker_panel_event` (orchs/base.py `apply_event`), unconditionally,
both live and on replay.

Before this fix, `orchestrator.Coordinator._finalize_turn_messages` ALSO
called `session_manager.snapshot_workers(app_session_id, msg_id, workers)`
at turn end, doing a WHOLESALE OVERWRITE `m["workers"] = snap`. `workers`
came from `turn_manager`'s `current_turn_workers` mirror, which was only
ever populated by the eager `apply_event(source_is_provider_stream=True)`
live-apply path — removed in the reconcile-elimination refactor. With that
mirror permanently empty, every turn that delegated to a worker had its
correctly-folded panels in `msg["workers"]` wiped to `[]` at finalize.

This test builds `msg["workers"]` the way the fold actually does (direct
`apply_event` calls for `worker_start` / `worker_event` / `worker_complete`,
mirroring what `SessionProjectionDrainer` does on the real async path), then
calls `_finalize_turn_messages` — the turn-end chokepoint — and asserts the
panel survives with all fold-written fields intact.

Run with:
    cd backend && .venv/bin/python scripts/test_worker_panel_survives_finalize.py
"""

from __future__ import annotations

import inspect
import os
import shutil
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-worker-panel-finalize-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from orchestrator import Coordinator  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _worker_event(delegation_id: str, uuid: str, text: str) -> dict:
    return {"type": "worker_event", "data": {
        "delegation_id": delegation_id,
        "event": {
            "type": "agent_message",
            "data": {
                "uuid": uuid,
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": text}]},
            },
        },
    }}


def _finalize(*, session, app_session_id, user_msg, assistant_msg, primary_result,
              stopped_at, trace_id):
    """Call `_finalize_turn_messages`, passing `workers=[]` ONLY if the
    (possibly pre-fix) signature still requires it — reproducing the
    real bug mechanism (turn_manager's mirror is always empty) rather
    than a signature-mismatch TypeError."""
    kwargs = dict(
        session=session, app_session_id=app_session_id, user_msg=user_msg,
        assistant_msg=assistant_msg, primary_result=primary_result,
        stopped_at=stopped_at, trace_id=trace_id,
    )
    if "workers" in inspect.signature(Coordinator._finalize_turn_messages).parameters:
        kwargs["workers"] = []
    Coordinator._finalize_turn_messages(object(), **kwargs)


def test_worker_panel_survives_finalize() -> None:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp/worker-panel-finalize",
        orchestration_mode="native",
    )
    sid = sess["id"]
    strategy = get_strategy("native")

    user_msg = {"id": "u1", "role": "user", "content": "delegate to a worker"}
    session_manager.append_user_msg(sid, user_msg)

    scaffold = strategy.build_assistant_scaffold()
    scaffold["id"] = "a1"
    session_manager.append_assistant_msg(sid, scaffold)
    msg = session_manager.get_ref(sid)["messages"][-1]
    ctx = ApplyEventCtx(root_id=session_manager._root_id_for(sid), run_id="run-1")

    # Mirrors the fold: worker_start creates the panel, worker_event
    # appends an inner frame, worker_complete merges terminal fields.
    # This is exactly what SessionProjectionDrainer does on the real
    # async path (orchs/base.py `apply_event`) — NOT a live-apply
    # shortcut, since that path was removed upstream of this test.
    strategy.apply_event(
        app_session_id=sid, msg=msg,
        event={"type": "worker_start", "data": {
            "delegation_id": "del-1",
            "worker_session_id": "ws-1",
            "worker_description": "sub-agent",
        }},
        ctx=ctx, source_is_provider_stream=True,
    )
    strategy.apply_event(
        app_session_id=sid, msg=msg,
        event=_worker_event("del-1", "uuid-1", "worker did the thing"),
        ctx=ctx, source_is_provider_stream=True,
    )
    strategy.apply_event(
        app_session_id=sid, msg=msg,
        event={"type": "worker_complete", "data": {
            "delegation_id": "del-1", "success": True,
        }},
        ctx=ctx, source_is_provider_stream=True,
    )
    session_manager.flush_pending_persists()

    pre_finalize = session_manager.get_ref(sid)["messages"][-1]
    pre_workers = pre_finalize.get("workers") or []
    assert len(pre_workers) == 1 and pre_workers[0].get("delegation_id") == "del-1", (
        f"setup: fold did not populate msg['workers'] before finalize — got {pre_workers!r}")

    _finalize(
        session=session_manager.get(sid),
        app_session_id=sid,
        user_msg=user_msg,
        assistant_msg=msg,
        primary_result={"success": True, "events": [], "sdk_output": ""},
        stopped_at=None,
        trace_id="trace-1",
    )

    after = session_manager.get(sid)
    saved = next(m for m in after["messages"] if m.get("id") == "a1")
    workers = saved.get("workers") or []

    # The parent assistant msg's `content` is projected from the PRIMARY
    # run's events/sdk_output (both empty here), never from a delegated
    # worker's output — worker output lives in the panel's `events` by
    # design. So this asserts only the panel-survival invariant: every
    # fold-written field of msg["workers"] survives the turn-end
    # chokepoint intact.
    assert len(workers) == 1, f"expected 1 worker panel, got {workers!r}"
    w = workers[0]
    assert w.get("delegation_id") == "del-1", f"delegation_id wrong: {w!r}"
    assert w.get("worker_session_id") == "ws-1", f"worker_session_id wrong: {w!r}"
    assert w.get("success") is True, f"success wrong: {w!r}"
    assert len(w.get("events") or []) == 1, f"expected 1 inner event, got {w.get('events')!r}"
    assert w["events"][0]["data"]["uuid"] == "uuid-1", f"inner uuid wrong: {w['events'][0]!r}"
    print(f"{PASS} worker panel survives _finalize_turn_messages "
          f"— got workers={workers!r}")


def main() -> int:
    try:
        test_worker_panel_survives_finalize()
        return 0
    except AssertionError as exc:
        print(f"{FAIL} {exc}")
        return 1
    except Exception:
        import traceback
        traceback.print_exc()
        return 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
