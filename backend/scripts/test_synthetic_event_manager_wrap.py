"""Regression test: synthetic SDK continuation markers ('No response
requested.', model '<synthetic>') must be filtered even when wrapped in
a `manager_event` envelope.

Bug: `is_synthetic_event` only matched BARE `agent_message` frames. In
manager orchestration the synthetic marker arrives one level deeper —
`{type:"manager_event", data:{event:{type:"agent_message", data:{...
model:"<synthetic>"}}}}` — so it bypassed the filter and rendered live
as a "No response requested." assistant turn. Fixed by unwrapping
`manager_event` one level in `is_synthetic_event` (commit e0a2978).

Asserts (the wrapped case FAILS on pre-fix code):
  1. wrapped manager_event synthetic → is_synthetic True.
  2. bare agent_message synthetic → is_synthetic True (unchanged).
  3. genuine manager_event with real assistant text → False (no
     over-filtering).
  4. strip_synthetic_events drops the wrapped synthetic, keeps the real
     manager_event.
  5. malformed / non-dict inputs → False (no crash).

Run with:
    cd backend && .venv/bin/python scripts/test_synthetic_event_manager_wrap.py
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from event_shape import is_synthetic_event, strip_synthetic_events  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

failures = 0


def _ok(cond: bool, label: str, detail: str = "") -> None:
    global failures
    if cond:
        print(f"{PASS}  {label}")
    else:
        print(f"{FAIL}  {label}  {detail}")
        failures += 1


def _agent_msg(*, model: str, text: str, api_err: bool = False) -> dict:
    return {
        "type": "agent_message",
        "data": {
            "type": "assistant",
            "isApiErrorMessage": api_err,
            "message": {
                "model": model,
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        },
    }


def _wrap_manager(inner: dict) -> dict:
    return {"type": "manager_event", "data": {"event": inner}}


bare_synth = _agent_msg(model="<synthetic>", text="No response requested.")
wrapped_synth = _wrap_manager(bare_synth)
bare_real = _agent_msg(model="claude-sonnet-4", text="here is the answer")
wrapped_real = _wrap_manager(bare_real)

_ok(is_synthetic_event(wrapped_synth) is True,
    "wrapped manager_event synthetic → filtered")
_ok(is_synthetic_event(bare_synth) is True,
    "bare agent_message synthetic → filtered")
_ok(is_synthetic_event(wrapped_real) is False,
    "genuine manager_event (real text) → NOT filtered")
_ok(is_synthetic_event(bare_real) is False,
    "bare real agent_message → NOT filtered")

kept = strip_synthetic_events([wrapped_synth, wrapped_real, bare_synth, bare_real])
_ok(kept == [wrapped_real, bare_real],
    "strip_synthetic_events drops both synthetics, keeps both real",
    f"kept={len(kept)}")

# Malformed / edge inputs must not crash and must be non-synthetic.
for bad in (None, "x", {}, {"type": "manager_event"},
            {"type": "manager_event", "data": {}},
            {"type": "manager_event", "data": {"event": None}},
            {"type": "manager_event", "data": {"event": "nope"}}):
    try:
        _ok(is_synthetic_event(bad) is False, f"malformed input → False: {bad!r}")
    except Exception as e:  # noqa: BLE001
        _ok(False, f"malformed input crashed: {bad!r}", str(e))


if __name__ == "__main__":
    raise SystemExit(1 if failures else 0)
