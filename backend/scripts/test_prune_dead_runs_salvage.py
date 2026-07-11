"""Regression tests: pruning a dead run salvages its on-disk complete.json.

A runner can die while NO live turn coroutine and NO recovery watcher own
its run (e.g. it exited after a mid-turn backend restart that never rehooked
it). `_prune_dead_entries` used to only flip isStreaming off, leaving the
assistant message unfinalized until the next backend restart. The prune path
must finalize the message from the run dir's complete.json via the canonical
`_apply_completion_state`, idempotently.

Run with:
    cd backend && .venv/bin/python scripts/test_prune_dead_runs_salvage.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from types import SimpleNamespace

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-prune-salvage-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from run_recovery import finalize_dropped_run_sync  # noqa: E402
from runs_dir import runs_root  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402
from turn_manager import TurnManager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _dead_pid() -> int:
    proc = subprocess.Popen(["/usr/bin/true"])
    proc.wait()
    return proc.pid


def _make_session_with_streaming_msg() -> tuple[str, str]:
    sess = session_manager.create(
        name="prune-salvage",
        model="codex",
        cwd="/tmp/prune-salvage",
        orchestration_mode="native",
        source="cli",
    )
    sid = sess["id"]
    msg_id = uuid.uuid4().hex
    session_manager.append_assistant_msg(sid, {
        "id": msg_id,
        "role": "assistant",
        "content": "",
        "events": [],
        "isStreaming": True,
    })
    return sid, msg_id


def _make_run_dir(payload: dict) -> str:
    run_id = str(uuid.uuid4())
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "complete.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )
    return run_id


def _msg(sid: str, msg_id: str) -> dict:
    sess = session_manager.get(sid) or {}
    for m in sess.get("messages") or []:
        if m.get("id") == msg_id:
            return m
    return {}


def _check(name: str, ok: bool) -> bool:
    print(f"{PASS if ok else FAIL}  {name}")
    return ok


def test_finalize_dropped_run_stamps_error() -> bool:
    sid, msg_id = _make_session_with_streaming_msg()
    run_id = _make_run_dir({"success": False, "error": "parent_gone"})
    changed = finalize_dropped_run_sync(
        persist_sid=sid, run_id=run_id, msg_id=msg_id,
    )
    m = _msg(sid, msg_id)
    ok = _check(
        "failed complete.json stamps assistant error",
        changed is True
        and m.get("error") is True
        and m.get("errorText") == "parent_gone"
        and not m.get("isStreaming"),
    )
    again = finalize_dropped_run_sync(
        persist_sid=sid, run_id=run_id, msg_id=msg_id,
    )
    ok = _check("second finalize is an idempotent no-op", again is False) and ok
    return ok


def test_finalize_dropped_run_stamps_success() -> bool:
    sid, msg_id = _make_session_with_streaming_msg()
    run_id = _make_run_dir({"success": True, "error": None})
    changed = finalize_dropped_run_sync(
        persist_sid=sid, run_id=run_id, msg_id=msg_id,
    )
    m = _msg(sid, msg_id)
    return _check(
        "successful complete.json stamps completed_at",
        changed is True
        and bool(m.get("completed_at"))
        and not m.get("error")
        and not m.get("isStreaming"),
    )


def test_finalize_without_complete_json_is_noop() -> bool:
    sid, msg_id = _make_session_with_streaming_msg()
    run_id = str(uuid.uuid4())
    (runs_root() / run_id).mkdir(parents=True)
    changed = finalize_dropped_run_sync(
        persist_sid=sid, run_id=run_id, msg_id=msg_id,
    )
    m = _msg(sid, msg_id)
    return _check(
        "missing complete.json leaves message untouched",
        changed is False and not m.get("error") and m.get("isStreaming"),
    )


def test_finalize_without_msg_id_fails_closed() -> bool:
    sid, msg_id = _make_session_with_streaming_msg()
    run_id = _make_run_dir({"success": False, "error": "stale_run"})
    changed = finalize_dropped_run_sync(
        persist_sid=sid, run_id=run_id, msg_id=None,
    )
    m = _msg(sid, msg_id)
    return _check(
        "missing msg_id fails closed (never stamps the last assistant)",
        changed is False and not m.get("error") and m.get("isStreaming"),
    )


def test_prune_dead_entry_salvages() -> bool:
    sid, msg_id = _make_session_with_streaming_msg()
    run_id = _make_run_dir({"success": False, "error": "runner_died"})
    tm = TurnManager(SimpleNamespace())
    tm._run_state[sid] = [{
        "run_id": run_id,
        "pid": _dead_pid(),
        "target_message_id": msg_id,
        "kind": "native",
    }]
    pruned = tm._prune_dead_entries(sid)
    for t in getattr(tm, "_salvage_threads", []):
        t.join(timeout=10)
    m = _msg(sid, msg_id)
    return _check(
        "prune finalizes dropped dead run from complete.json",
        pruned is True
        and m.get("error") is True
        and m.get("errorText") == "runner_died"
        and not m.get("isStreaming"),
    )


def main() -> int:
    ok = True
    for fn in (
        test_finalize_dropped_run_stamps_error,
        test_finalize_dropped_run_stamps_success,
        test_finalize_without_complete_json_is_noop,
        test_finalize_without_msg_id_fails_closed,
        test_prune_dead_entry_salvages,
    ):
        try:
            ok = fn() and ok
        except Exception as exc:  # noqa: BLE001
            print(f"{FAIL}  {fn.__name__} raised: {exc!r}")
            ok = False
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
