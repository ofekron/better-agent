"""Feature flow: a full chat turn — send a message and get an assistant reply.

Exercises the core conversation loop on a fresh session:
  create_new_session (initial prompt) -> wait for first reply ->
  send_message (follow-up)            -> wait for a NEW reply.

Validates the reusable composer subflows (send_message, wait_assistant_reply)
and proves a second assistant bubble appears after the follow-up, i.e. the
turn actually completed and rendered, not just the user message.
"""
import os
import sys

_LIB = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, _LIB)
import bc  # noqa: E402

from testape_engine.flow_builder import FlowBuilder  # noqa: E402


def build_flow(adapter_id, base_url=bc.APP_BASE):
    fb = FlowBuilder(
        name="chat__send_and_reply",
        adapter_id=adapter_id,
        folder="bc/features",
        tags=["smoke", "chat"],
    )
    fb.subflow(bc.create_new_session(adapter_id, base_url=base_url))
    fb.subflow(bc.wait_assistant_reply(adapter_id, min_messages=1))
    fb.subflow(bc.send_message(adapter_id))
    fb.subflow(bc.wait_assistant_reply(adapter_id, min_messages=2))
    return fb.build()
