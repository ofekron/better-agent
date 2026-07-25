"""Regression: model_switched must survive message stubbing.

Completed assistant messages ship stubbed on reload (`events: []` with a
preview in `stub.last_events`). A model-switch badge that renders live must
still be present in the stub preview after reload, otherwise it disappears.

`model_switched` is a turn-boundary marker (like `steer_prompt`), so it is
pinned into the stub preview regardless of the 25-event tail — a long turn
would otherwise push it out of the collapsed preview.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import render_stub  # noqa: E402


def _agent_event(idx: int) -> dict:
    return {"type": "agent_message", "data": {"type": "assistant", "uuid": f"a-{idx}", "message": {"content": [{"type": "text", "text": f"tok{idx}"}]}}}


def _model_switch_event() -> dict:
    return {
        "type": "model_switched",
        "data": {"uuid": "model-switch-1", "model": "model-b", "previous_model": "model-a"},
    }


def run() -> None:
    # model_switched at the HEAD of a >STUB_TAIL event list: the tail alone
    # would drop it, pinning must keep it in the preview.
    events = [_model_switch_event()] + [_agent_event(i) for i in range(render_stub.STUB_TAIL + 5)]
    msg = {"role": "assistant", "events": events}
    stub = render_stub.build_stub(msg)

    assert stub["event_count"] == len(events), stub
    types = [e.get("type") for e in stub["last_events"]]
    assert "model_switched" in types, f"model_switched dropped from stub preview: {types}"

    # Sanity: a second model_switch is also retained (pinning keeps all).
    events2 = (
        [_model_switch_event()]
        + [_agent_event(i) for i in range(render_stub.STUB_TAIL + 5)]
        + [
            {
                "type": "model_switched",
                "data": {"uuid": "model-switch-2", "model": "model-c", "previous_model": "model-b"},
            }
        ]
    )
    stub2 = render_stub.build_stub({"role": "assistant", "events": events2})
    ms = [e for e in stub2["last_events"] if e.get("type") == "model_switched"]
    assert len(ms) == 2, f"expected both model_switched pinned, got {len(ms)}"

    print("test_stub_pins_model_switched: OK")


if __name__ == "__main__":
    run()
