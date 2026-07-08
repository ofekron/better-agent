"""Regression test: rearranger flag-check must not deepcopy the session.

`rearranger.trigger_final` (fired as a background task after every turn) and
`_ticker_loop` (every ~20s) read the `rearranger_enabled` boolean. Both used
to call `session_manager.get(sid)`, which deep-copies the ENTIRE live session
tree for caller-isolation — just to read one bool. On large sessions this
blocked the asyncio loop for hundreds of ms to seconds (faulthandler-confirmed:
`copy.deepcopy` via `session_manager.py:get` <- `rearranger.py:trigger_final`,
dumps at 1.7s–2.3s on production-sized sessions).

The fix reads the single field via `session_manager.is_rearranger_enabled`
(no deepcopy). This test pins that `trigger_final`'s flag-check path:
  (a) does NOT call `session_manager.get` (the deepcopy method), and
  (b) completes well under the deepcopy cost (482ms measured pre-fix on a
      4000-message session; the fixed path measures ~tens of ms warm).

Run with:
    cd backend && .venv/bin/python scripts/test_rearranger_flag_check_no_deepcopy.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_test_home.isolate("bc-test-rearranger-flag-")

from session_manager import manager as session_manager  # noqa: E402
import rearranger  # noqa: E402

N_MESSAGES = 4000
MSG_BODY = "x" * 2000
# Pre-fix `get()` deepcopy of this session measured 482ms; the fixed flag
# read measures ~tens of ms warm. Bound at 150ms — any regression that
# reintroduces a full-session deepcopy (482ms) blows past it with margin.
TIMEOUT_MS = 150


def _seed_session(sid: str, enabled: bool) -> None:
    def _do(s: dict) -> None:
        s["messages"] = [
            {
                "id": f"m-{i}",
                "role": "user",
                "content": MSG_BODY,
                "seq": i,
                "timestamp": "2026-07-08T00:00:00",
            }
            for i in range(N_MESSAGES)
        ]
        s["rearranger_enabled"] = enabled
    session_manager._run(sid, _do, {"kind": "seed_test"})


class _GetCallCounter:
    """Wraps `session_manager.get` to count calls on the entering thread."""

    def __init__(self) -> None:
        self.count = 0
        self._real = session_manager.get
        self._thread = None

    def __enter__(self) -> "_GetCallCounter":
        import threading
        self.count = 0
        self._thread = threading.current_thread()
        real = self._real
        owner = self

        def _counting(sid, *args, **kwargs):
            if threading.current_thread() is owner._thread:
                owner.count += 1
            return real(sid, *args, **kwargs)

        session_manager.get = _counting
        return self

    def __exit__(self, *exc) -> None:
        session_manager.get = self._real


def main() -> int:
    try:
        dis = session_manager.create(
            name="rearranger-flag-disabled", model="sonnet", cwd="/tmp/r-dis",
            orchestration_mode="native", source="test",
        )
        dis_sid = dis["id"]
        _seed_session(dis_sid, enabled=False)

        en = session_manager.create(
            name="rearranger-flag-enabled", model="sonnet", cwd="/tmp/r-en",
            orchestration_mode="native", source="test",
        )
        en_sid = en["id"]
        _seed_session(en_sid, enabled=True)

        rg = rearranger.Rearranger(session_manager)

        # Warm the root cache so the measured call reflects steady-state
        # per-call cost, not the one-time cold `_load_root` hydration.
        session_manager.get_field(dis_sid, "id")
        session_manager.get_field(en_sid, "id")

        # 1. is_rearranger_enabled returns correct values.
        assert session_manager.is_rearranger_enabled(dis_sid) is False
        assert session_manager.is_rearranger_enabled(en_sid) is True
        assert session_manager.is_rearranger_enabled("nope-does-not-exist") is False

        # 2. trigger_final on the DISABLED large session: the flag check must
        #    NOT call session_manager.get (which deep-copies the whole session)
        #    and must complete well under the pre-fix deepcopy cost.
        with _GetCallCounter() as gc:
            t0 = time.perf_counter()
            asyncio.run(rg.trigger_final(dis_sid))
            tf_ms = (time.perf_counter() - t0) * 1000
        assert gc.count == 0, (
            f"trigger_final called session_manager.get {gc.count} time(s) — "
            f"the flag check must read the field without deepcopying the session"
        )
        assert tf_ms < TIMEOUT_MS, (
            f"trigger_final took {tf_ms:.1f}ms (> {TIMEOUT_MS}ms) on a "
            f"{N_MESSAGES}-message session — likely reintroduced a full-session "
            f"deepcopy (pre-fix get() was 482ms)"
        )

        print(
            f"OK: trigger_final {tf_ms:.1f}ms, 0 get() calls on "
            f"{N_MESSAGES}-msg session (pre-fix get() was 482ms)"
        )
        return 0
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
