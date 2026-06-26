"""Feature flow: create a brand new session via the New Session modal.

Exercises the primary entry point of the app and validates the reusable
modal subflows (open_app -> open_new_session_modal -> submit_new_session).
"""
import os
import sys

_LIB = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, _LIB)
import bc  # noqa: E402

from testape_engine.flow_builder import FlowBuilder  # noqa: E402


def build_flow(adapter_id, base_url=bc.APP_BASE):
    fb = FlowBuilder(
        name="session__create_new_session",
        adapter_id=adapter_id,
        folder="bc/features",
        tags=["smoke", "session"],
    )
    fb.subflow(bc.open_app(adapter_id, base_url))
    fb.subflow(bc.open_new_session_modal(adapter_id))
    fb.subflow(bc.submit_new_session(adapter_id))
    return fb.build()
