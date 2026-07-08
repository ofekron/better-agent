"""Regression tests for the per-turn `run_meta` stamp on assistant messages.

Pins:
  1. `Coordinator._build_assistant_msg` stamps `run_meta` with the resolved
     provider_id / model / reasoning_effort used for the turn, preferring
     explicit per-turn overrides over the session record.
  2. Falls back to the session record when overrides are absent (the model
     picker writes the session before the turn runs) — matches
     `_drive_cli_run` resolution.
  3. Omits `run_meta` entirely when nothing is resolvable, so old turns
     render without a badge rather than with empty chips.
  4. `run_meta` survives `session_manager.get_lite` (only events are
     stripped), so the badge reaches the frontend via thin snapshots too.

Run with:
    cd backend && .venv/bin/python scripts/test_assistant_run_meta.py
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402
_TMP_HOME = _test_home.isolate("bc-test-assistant-run-meta-")

from orchestrator import Coordinator  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

# `_build_assistant_msg` reads no instance state, so a bare object stands
# in for the Coordinator — avoids a full Coordinator construction.
_COORD = object.__new__(Coordinator)


def _build(session: dict, **kwargs) -> dict:
    return Coordinator._build_assistant_msg(_COORD, session=session, **kwargs)


def test_override_wins_over_session() -> None:
    session = {"id": "s1", "orchestration_mode": "native",
               "provider_id": "p-session", "model": "m-session",
               "reasoning_effort": "low"}
    msg = _build(
        session, app_session_id="s1",
        provider_id="p-turn", model="m-turn", reasoning_effort="high",
    )
    assert msg["run_meta"] == {
        "provider_id": "p-turn", "model": "m-turn", "reasoning_effort": "high",
    }, msg.get("run_meta")
    print(f"{PASS} per-turn override stamps over session values")


def test_falls_back_to_session() -> None:
    session = {"id": "s2", "orchestration_mode": "native",
               "provider_id": "p-session", "model": "m-session",
               "reasoning_effort": "medium"}
    msg = _build(session, app_session_id="s2", model="m-turn")
    # No provider_id / reasoning_effort override → session values used.
    assert msg["run_meta"] == {
        "provider_id": "p-session", "model": "m-turn",
        "reasoning_effort": "medium",
    }, msg.get("run_meta")
    print(f"{PASS} missing overrides fall back to the session record")


def test_omitted_when_unresolvable() -> None:
    session = {"id": "s3", "orchestration_mode": "native"}
    msg = _build(session, app_session_id="s3")
    assert "run_meta" not in msg, msg.get("run_meta")
    print(f"{PASS} run_meta omitted when nothing resolvable")


def test_survives_get_lite() -> None:
    sess = session_manager.create(
        name="t", model="sonnet", cwd="/tmp",
        orchestration_mode="native", source="cli",
        provider_id="p-live",
    )
    sid = sess["id"]
    msg = _build(
        sess, app_session_id=sid,
        provider_id="p-live", model="sonnet", reasoning_effort="high",
    )
    session_manager.append_assistant_msg(sid, msg)

    lite = session_manager.get_lite(sid)
    assert lite is not None, "get_lite returned None"
    persisted = lite["messages"][-1]
    assert persisted.get("run_meta") == {
        "provider_id": "p-live", "model": "sonnet", "reasoning_effort": "high",
    }, persisted.get("run_meta")
    print(f"{PASS} run_meta survives get_lite (thin snapshot)")


def main() -> int:
    failures = 0
    for fn in (
        test_override_wins_over_session,
        test_falls_back_to_session,
        test_omitted_when_unresolvable,
        test_survives_get_lite,
    ):
        try:
            fn()
        except AssertionError as exc:
            print(f"{FAIL} {fn.__name__}: {exc}")
            failures += 1
        except Exception:
            print(f"{FAIL} {fn.__name__} raised:")
            import traceback
            traceback.print_exc()
            failures += 1
    if failures:
        print(f"\n{FAIL} {failures} test(s) failed")
        return 1
    print(f"\n{PASS} all run_meta tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
